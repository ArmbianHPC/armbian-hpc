#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Refresh normal/Rescue software while preserving an explicit fixed BOOT and matching modules."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess

ROLES = ('normal', 'rescue', 'public/system', 'public/rescue')
DTBS = {'wxy-oect-original': 'dtb/rockchip/rk3566-wxy-oec-turbo-4g.dtb',
        'wxy-oesp-v22': 'dtb/amlogic/meson-g12b-s922x-armhpc-oes1plus-v22.dtb'}


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_pair(work, manifest, board):
    if board not in DTBS or set(manifest.get('files', {})) != {'kernel', 'dtb', 'initramfs'}:
        raise ValueError('Unsupported board or incomplete fixed BOOT manifest')
    root = Path(work).resolve(strict=True)
    kernel = manifest['kernel_release']
    if not kernel or '/' in kernel or kernel in ('.', '..'):
        raise ValueError('Invalid kernel release')
    for role, name in (('kernel', 'Image'), ('dtb', DTBS[board]), ('initramfs', 'uInitrd')):
        path = (root / 'boot' / name).resolve(strict=True)
        if not path.is_relative_to(root / 'boot') or sha(path) != manifest['files'][role]['sha256']:
            raise ValueError('Fixed BOOT pairing differs: ' + role)
    for role in ROLES:
        modules = root / role / 'lib/modules' / kernel
        if not modules.is_dir() or (modules / 'armhpc-kernel-build-id').read_text().strip() != manifest['files']['kernel']['sha256']:
            raise ValueError('Matching modules are missing: ' + role)
    return manifest


def module_files(root, kernel):
    directory = Path(root) / 'lib/modules' / kernel
    return {p.relative_to(directory).as_posix(): sha(p) for p in directory.rglob('*')
            if p.is_file() and not p.is_symlink()}


def load(path):
    spec = importlib.util.spec_from_file_location('firmware_framework_resources', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def refresh(project, ota_source, input_roots, boot_manifest, work, watchdog_binary,
            board, release, epoch):
    project, ota_source, input_roots, work = (Path(p).resolve() for p in (project, ota_source, input_roots, work))
    if os.name == 'nt' or os.geteuid() != 0:
        raise ValueError('Run offline root preparation as Linux root')
    if work.exists() or work == Path('/') or any((p / '.git').exists() for p in (work, *work.parents)):
        raise ValueError('Use a fresh work directory outside source repositories')
    manifest = json.loads(Path(boot_manifest).read_text())
    validate_pair(input_roots, manifest, board)
    before_modules = {role: module_files(input_roots / role, manifest['kernel_release']) for role in ROLES}
    resources = load(ota_source / 'tools/package-runtime.py').framework_files(project)
    work.mkdir(parents=True)
    for name in ('normal', 'rescue', 'public', 'boot', 'rescue-defaults'):
        subprocess.run(['cp', '-a', '--reflink=auto', str((input_roots / name).resolve(strict=True)), str(work / name)], check=True)
    common = project / 'components/common/rootfs-overlay'
    for role in ROLES:
        root = work / role
        rescue = role.endswith('rescue')
        scope = 'rescue' if rescue else 'normal'
        for source, item in resources:
            if item['scope'] not in ('common', scope, 'board') or (item['scope'] == 'board' and item.get('board') != board):
                continue
            target = root / item['target']
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                target.unlink()
            shutil.copyfile(source, target)
            target.chmod(0o644)
        for unit in ('armhpc-clock-floor.service', 'armhpc-clock-checkpoint.service',
                     'armhpc-clock-checkpoint.timer', 'armhpc-clock-shutdown.service'):
            shutil.copy2(common / 'etc/systemd/system' / unit, root / 'etc/systemd/system' / unit)
        relative = 'etc/systemd/system/armhpc-firstboot.service.d/20-armhpc-clock.conf'
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(common / relative, root / relative)
        subprocess.run(['python3', str(ota_source / 'tools/install-runtime.py'), '--root', str(root),
            '--watchdog-binary', str(watchdog_binary), '--bootstrap-hooks'], check=True)
        layout = runpy.run_path(str(root / 'usr/lib/armhpc/command_layout.py'))
        layout['finalize'](root, 'rescue' if rescue else 'normal')
        units = ['armhpc-clock-floor.service', 'armhpc-clock-checkpoint.timer', 'armhpc-clock-shutdown.service',
                 'armhpc-performance.timer']
        if board == 'wxy-oesp-v22':
            units.append('armhpc-fan.service')
        subprocess.run(['systemctl', '--root=' + str(root), 'enable', *units], check=True)
        for name, value in (('release.json', {'release': release, 'target': 'rescue' if rescue else 'system',
                                             'kernel': manifest['kernel_release']}),
                            ('image-build-time.json', {'epoch': epoch, 'revision': release})):
            (root / 'etc/armhpc' / name).write_text(json.dumps(value, indent=2) + '\n')
        document = root / 'usr/share/doc/armhpc/image-release.md'
        document.parent.mkdir(parents=True, exist_ok=True)
        version = (project / 'VERSION').read_text().strip()
        document.write_text('<!-- SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only -->\n'
            '# ARMHPC ' + version + '\n\nRuntime and board integration use matching project sources. '
            'Kernel, DTB, raw initramfs and modules retain the supplied fixed BOOT pairing.\n')
        for path in (root / 'usr/lib/armhpc-ota').rglob('__pycache__'):
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        if module_files(root, manifest['kernel_release']) != before_modules[role]:
            raise ValueError('The module tree changed during runtime refresh: ' + role)
    validate_pair(work, manifest, board)
    output = {**manifest, 'release': release}
    (work / 'boot-manifest.json').write_text(json.dumps(output, indent=2) + '\n')
    report = {'release': release, 'fixed_boot_retained': True, 'boot': output,
              'modules_unchanged': True, 'roles': list(ROLES), 'hardware_verified': False}
    (work / 'preparation.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('project', 'ota-source', 'input-roots', 'boot-manifest', 'work', 'watchdog-binary'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--board', choices=DTBS, required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--epoch', type=int, required=True)
    print(json.dumps(refresh(**vars(parser.parse_args())), indent=2))
