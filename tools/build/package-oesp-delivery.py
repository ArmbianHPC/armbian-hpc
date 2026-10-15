#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Package a completed local OESP image and its signed OTA payloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('firmware', 'ota-source', 'signing-key', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--key-id', required=True)
    parser.add_argument('--epoch', type=int, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.ota_source / 'src'))
    from armhpc_ota.metadata import sign, verify, validate_manifest
    from armhpc_ota.io import canonical
    from armhpc_ota.local_package import inspect
    assert not args.output.exists()
    args.output.mkdir(parents=True)
    compressed = list(args.firmware.glob('armhpc_*.img.xz'))
    assert len(compressed) == 1
    image = compressed[0].with_suffix('')
    manifests = list((args.firmware / 'ota').glob('*.manifest.json'))
    assert len(manifests) == 1
    envelope = json.loads(manifests[0].read_text())
    manifest = validate_manifest(envelope['payload'])
    assert manifest['board'] == 'wxy-oesp-v22' and manifest['layout'] == 'oesp-mbr-squashfs-ab-v1'
    transport = args.output / 'ota-package'
    transport.mkdir()
    (transport / 'manifest.json').write_bytes(manifests[0].read_bytes())
    blobs = transport / 'blobs'
    blobs.mkdir()
    for item in manifest['artifacts']:
        source = args.firmware / 'ota' / item['url'].rsplit('/', 1)[-1]
        assert source.stat().st_size == item['size'] and digest(source) == item['sha256']
        destination = blobs / item['sha256']
        if not destination.exists():shutil.copy2(source, destination)
    package = args.output / compressed[0].name.replace('armhpc_', 'armhpc-ota_', 1).replace('.img.xz', '.tar.xz')
    with tarfile.open(package, 'x:xz', preset=6) as archive:
        for path in sorted(transport.rglob('*')):
            if not path.is_file():continue
            info = archive.gettarinfo(str(path), path.relative_to(transport).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mode = 0o644
            info.mtime = args.epoch
            with path.open('rb') as stream:archive.addfile(info, stream)
    local = inspect(package, args.output / 'verified-package')
    assert local['release'] == manifest['release']
    image_manifest = {'protocol': 1, 'kind': 'image', 'release': manifest['release'], 'board': manifest['board'],
        'architecture': 'arm64', 'layout': manifest['layout'], 'anchor_abi': manifest['anchor_abi'],
        'image': {'bytes': image.stat().st_size, 'sha256': digest(image)}, 'hardware_verified': False}
    image_manifest_path = args.output / 'image.manifest.json'
    image_manifest_path.write_bytes(sign(image_manifest, args.signing_key, args.key_id))
    destination=args.output / compressed[0].name
    with compressed[0].open('rb') as source,destination.open('xb') as target:
        shutil.copyfileobj(source,target,1024**2)
        target.flush();os.fsync(target.fileno())
    assert digest(destination)==digest(compressed[0]),'Delivered compressed image hash differs'
    for path in args.firmware.glob('*.json'):
        if path.name in ('BUILD-RESULT.json', 'IMAGE-AUDIT.json', 'compression-verification.json', 'compression-comparison.json'):
            shutil.copy2(path, args.output / path.name)
    files = [p for p in args.output.iterdir() if p.is_file()]
    receipt = {'release': manifest['release'], 'hardware_verified': False,
        'raw_image': image_manifest['image'], 'files': [{'file': p.name, 'bytes': p.stat().st_size, 'sha256': digest(p)} for p in sorted(files)]}
    (args.output / 'DELIVERY.json').write_text(json.dumps(receipt, indent=2) + '\n')
    (args.output / 'SHA256SUMS').write_text(''.join(r['sha256'] + '  ' + r['file'] + '\n' for r in receipt['files']))
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
