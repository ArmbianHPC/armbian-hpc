#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Bundle task-bound USB Rescue recovery with the isolated Windows Python helper."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import zipfile


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('publisher', 'ota-source', 'cache', 'skeleton', 'public-key', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    assert not args.output.exists() and not any((p / '.git').exists() for p in (args.output, *args.output.parents))
    root = args.output / 'ARMHPC-OESP-USB'
    root.mkdir(parents=True)
    python = root / 'python'
    shutil.copytree(args.publisher, python, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    lib = python / 'Lib'
    core = lib / 'armhpc_ota'
    if core.exists():
        assert core.resolve().parent == lib.resolve()
        shutil.rmtree(core)
    shutil.copytree(args.ota_source / 'src/armhpc_ota', core, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    lock_path = Path(__file__).with_name('oesp-usb-dependencies.json')
    lock = json.loads(lock_path.read_text())
    archive = args.cache / lock['pyamlboot']['file']
    assert digest(archive) == lock['pyamlboot']['sha256']
    licenses = root / 'LICENSES'
    licenses.mkdir()
    with tarfile.open(archive, 'r:gz') as package:
        for member in package.getmembers():
            parts = Path(member.name).parts[1:]
            if not member.isfile() or not parts:
                continue
            assert '..' not in parts
            if parts[0] == 'pyamlboot':
                destination = lib / Path(*parts)
            elif parts[0].startswith('LICENSE'):
                destination = licenses / ('pyamlboot-' + parts[0])
            else:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(package.extractfile(member).read())
    for wheel in lock['wheels']:
        archive = args.cache / wheel['file']
        assert digest(archive) == wheel['sha256']
        with zipfile.ZipFile(archive) as package:
            package.extractall(lib)
            for name in package.namelist():
                if not name.endswith('/') and ('LICENSE' in name.upper() or 'COPYING' in name.upper()):
                    target = licenses / wheel['name'] / Path(name).name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(package.read(name))
    linux = root / 'linux/Lib'
    linux.mkdir(parents=True)
    for name in ('armhpc_ota', 'pyamlboot', 'usb'):
        shutil.copytree(lib / name, linux / name)
    inherited = args.publisher.parent / 'LICENSES/third-party'
    if inherited.is_dir():shutil.copytree(inherited, licenses / 'publisher-dependencies')
    loader = root / 'loader'
    loader.mkdir()
    for name in ('DDR.USB','DDR_ENC.USB','UBOOT.USB','UBOOT_ENC.USB','platform.conf'):
        shutil.copy2(args.skeleton / name, loader / name)
    config='[LIST_NORMAL]\n'+''.join(f'file="{name}" main_type="USB" sub_type="{role}" file_type="normal"\n'
        for name,role in (('DDR.USB','DDR'),('DDR_ENC.USB','DDR_ENC'),('UBOOT.USB','UBOOT'),('UBOOT_ENC.USB','UBOOT_ENC')))
    config+='file="platform.conf" main_type="conf" sub_type="platform" file_type="normal"\n[LIST_VERIFY]\n'
    (loader/'image.cfg').write_text(config,encoding='ascii',newline='\n')
    trust = root / 'trust'
    trust.mkdir()
    shutil.copy2(args.public_key, trust / args.public_key.name)
    for name in ('oesp-usb-rescue.py','oesp-usb-diagnose.py','usb_diag_handoff.py','usb_diag_stream.py','usb_diag_upload.py','usb_diag_fat.py'):
        shutil.copy2(project/'tools'/name,root/name)
    (root/'oesp-usb-rescue.ps1').write_text('''# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
param([Parameter(Mandatory=$true)][string]$Export,[switch]$Prepare)
$ArmHpcOperation=if($Prepare){'prepare'}else{'boot'}
& (Join-Path $PSScriptRoot 'python/python.exe') -B (Join-Path $PSScriptRoot 'oesp-usb-rescue.py') $ArmHpcOperation --export $Export --load-ram --yes
exit $LASTEXITCODE
''',encoding='utf-8')
    (root/'oesp-usb-rescue.sh').write_text('''#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
set -eu
base=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$base/linux/Lib${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "$base/oesp-usb-rescue.py" "$@"
''')
    (root/'oesp-usb-rescue.sh').chmod(0o755)
    shutil.copy2(project / 'docs/USB-RECOVERY.md', root / 'README.md')
    shutil.copy2(project / 'docs/USB-RECOVERY.zh-CN.md', root / 'README.zh-CN.md')
    for name in ('USB-RECOVERY.md','USB-RECOVERY.zh-CN.md'):
        shutil.copy2(project/'docs'/name,root/name)
    shutil.copy2(args.ota_source / 'LICENSE', root / 'LICENSE')
    shutil.copytree(args.ota_source / 'LICENSES', licenses / 'armhpc-ota')
    shutil.copy2(lock_path, root / 'DEPENDENCIES.json')
    (root / 'THIRD-PARTY-NOTICES.md').write_text('''# USB transport dependencies

Pyamlboot module-specific MIT alternatives are selected. PyUSB retains its BSD license.
libusb-package and the embedded libusb retain their original licenses, including LGPL-2.1-or-later for libusb.
The copied Python publishing helper retains its CPython, cryptography, OpenSSL, cffi and other original notices.
Exact package hashes and complete upstream license files are included.
Vendor loader and DTB are private local inputs; public redistribution has not been approved.
No vendor key, password, SECURE_BOOT_SET or device backup is included.
''')
    shutil.copy2(args.ota_source / 'THIRD-PARTY-NOTICES.md', root / 'OTA-THIRD-PARTY-NOTICES.md')
    version=(project/'VERSION').read_text().strip()
    result = {'board': 'wxy-oesp-v22', 'version': version, 'hardware_verified': False,
              'vendor_inputs_private': True, 'files': [{'path': p.relative_to(root).as_posix(), 'sha256': digest(p)}
              for p in sorted(root.rglob('*')) if p.is_file()]}
    (root / 'BUILD-INFO.json').write_text(json.dumps(result, indent=2) + '\n')
    destination = args.output / ('armhpc-oesp-'+version+'-usb-tools.zip')
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob('*')):
            if path.is_file(): archive.write(path, root.name + '/' + path.relative_to(root).as_posix())
    print(json.dumps({'path': str(destination), 'bytes': destination.stat().st_size, 'sha256': digest(destination)}))


if __name__ == '__main__':
    main()
