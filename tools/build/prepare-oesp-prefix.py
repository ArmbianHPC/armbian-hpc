#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only
"""Create an audited local 100 MiB OESP prefix from pinned private inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from framework.amlogic_boot import decode_environment, environment_text, validate_vendor_environment

FIP_SHA256 = '2e28a6d05bcda412195d4991beba403cc6fe6859d7770ebead91a34990c48fde'
AMPART_SOURCE_SHA256 = '2421cd79812e68d8623cf05aeb367f7c701c47eba76f6794fb164672233e9115'
EPT = 'bootloader:0:4194304:0 env:4194304:8388608:0 reserved:37748736:67108864:0'
MINIMAL_RESERVED_SHA256 = 'e8946f1e4fafb0ef39b3452a560630a8384aa9e5bd243b950a325f59cce5d717'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_environment(block):
    return decode_environment(block)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('skeleton', 'ampart', 'ampart-source', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    assert digest(args.ampart_source) == AMPART_SOURCE_SHA256
    fip = args.skeleton / 'DDR.USB'
    assert fip.stat().st_size == 2097152 and digest(fip) == FIP_SHA256
    args.output.mkdir(parents=True, exist_ok=True)
    prefix = args.output / 'oesp-prefix.img'
    assert not prefix.exists(), 'Build a new prefix directory; existing inputs are immutable'
    with prefix.open('xb') as stream:
        stream.truncate(100 * 1024**2)
    for mode, rest, accepted in [('ecreate', [], (0, 35)), ('eedit', ['^cache?'], (0, 34))]:
        result = subprocess.run([str(args.ampart), str(prefix), '--mode', mode, *rest], capture_output=True, text=True)
        assert result.returncode in accepted, 'EPT operation failed before the blank-DTB phase'
    snapshot = subprocess.check_output([str(args.ampart), str(prefix), '--mode', 'esnapshot'], text=True)
    assert snapshot.splitlines()[0] == EPT, 'EPT geometry differs'
    environment = args.output / 'armhpc-env.txt'
    environment.write_text(environment_text(), encoding='ascii')
    envbin = args.output / 'armhpc-env.bin'
    subprocess.run(['mkenvimage', '-s', '65536', '-o', str(envbin), str(environment)], check=True)
    block = envbin.read_bytes()
    entry = validate_vendor_environment(block)
    with prefix.open('r+b') as target:
        target.seek(512)
        target.write(fip.read_bytes())
        target.seek(4 * 1024**2)
        target.write(envbin.read_bytes())
        target.write(block)
        # The minimal EPT must not contain the vendor Android partition DTB.
        target.seek(36 * 1024**2)
        assert hashlib.sha256(target.read(64 * 1024**2)).hexdigest() == MINIMAL_RESERVED_SHA256, 'Minimal reserved EPT differs'
    components = []
    with prefix.open('rb') as stream:
        for name, offset, size, origin, reviewed in [
            ('vendor-fip', 512, 2097152, 'Pinned OESP vendor FIP', False),
            ('boot-environment', 4 * 1024**2, 8 * 1024**2, 'ARMHPC generated environment', True),
            ('reserved-ept', 36 * 1024**2, 64 * 1024**2, 'Generated minimal EPT without an Android partition DTB', True)]:
            stream.seek(offset)
            components.append({'id': name, 'offset_bytes': offset, 'size_bytes': size,
                               'sha256': hashlib.sha256(stream.read(size)).hexdigest(),
                               'source': origin, 'redistributable': reviewed})
    result = {'permanent_boot': components, 'prefix_sha256': digest(prefix), 'fip_hardware_verified': False,
              'private_local_image': True, 'ampart_source_sha256': AMPART_SOURCE_SHA256,
              'minimal_reserved_sha256': MINIMAL_RESERVED_SHA256, 'vendor_partition_dtb_present': False,
              'environment': {'header_bytes': 4, 'block_bytes': 65536, 'copy_offsets': [4194304, 4259840],
                              'sha256': hashlib.sha256(block).hexdigest(), **entry}}
    (args.output / 'PREFIX-PROVENANCE.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
