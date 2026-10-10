#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Read-only audit of a built rootfs. Also copied into the image build chroot."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def packages(root):
    status = root / 'var/lib/dpkg/status'
    if not status.is_file() or not status.resolve().is_relative_to(root.resolve()):
        raise ValueError('缺少根文件系统中的 dpkg/status。')
    result = []
    for paragraph in status.read_text(encoding='utf-8').split('\n\n'):
        record = {}
        for line in paragraph.splitlines():
            if line and not line[0].isspace() and ': ' in line:
                name, value = line.split(': ', 1)
                record[name] = value
        if record.get('Status') != 'install ok installed':
            continue
        source = re.fullmatch(r'(\S+)(?: \(([^)]+)\))?', record.get('Source', record['Package']))
        if source is None:
            raise ValueError('dpkg Source 字段无效。')
        result.append({'package': record['Package'], 'version': record['Version'],
                       'source': source[1], 'source_version': source[2] or record['Version']})
    if not result:
        raise ValueError('安装包清单为空。')
    return sorted(result, key=lambda item: item['package'])


def runtime_binary_data(path, relative, package_names):
    """Recognize specific runtime data and modules, never arbitrary .bin files."""
    if path.is_symlink():
        return False
    module = re.fullmatch(r'(?:usr/)?lib/modules/([^/]+)/(.+)', relative)
    has_kernel = any(name.startswith('linux-image-') for name in package_names)
    if module and has_kernel:
        release, name = module.groups()
        if not re.fullmatch(r'[0-9][A-Za-z0-9_.+-]*', release):
            return False
        if name in ('modules.alias.bin', 'modules.builtin.alias.bin', 'modules.builtin.bin',
                    'modules.dep.bin', 'modules.symbols.bin') and 'kmod' in package_names:
            with path.open('rb') as stream:
                header = stream.read(12)
            # kmod's index format is big endian: magic, version, flagged root offset.
            return (len(header) == 12 and header[:4] == bytes.fromhex('b007f457')
                    and int.from_bytes(header[4:8], 'big') >> 16 == 2
                    and int.from_bytes(header[8:12], 'big') & 0x0fffffff < path.stat().st_size)
        if name in ('kernel/drivers/nvmem/nvmem_u-boot-env.ko',
                    'kernel/drivers/nvmem/layouts/u-boot-env.ko'):
            data = path.read_bytes()
            # These are Linux ET_REL modules that read U-Boot environment data.
            return (len(data) >= 20 and data[:4] == b'\x7fELF' and data[5] == 1
                    and int.from_bytes(data[16:18], 'little') == 1
                    and int.from_bytes(data[18:20], 'little') in (40, 183)
                    and b'.modinfo' in data and b'vermagic=' + release.encode() + b' ' in data)
    if relative == 'usr/lib/udev/hwdb.bin' and 'udev' in package_names:
        with path.open('rb') as stream:
            header = stream.read(80)
        return (len(header) == 80 and header[:8] == b'KSLPHHRH'
                and int.from_bytes(header[16:24], 'little') == path.stat().st_size)
    return False


def audit_rootfs(directory, policy, *, build_overlay=False):
    root = Path(directory).resolve()
    errors = []
    inventory = packages(root)
    package_names = {item['package'] for item in inventory}
    def unreadable(error):
        raise ValueError('不能读取镜像目录：' + str(error.filename))
    allowed = {item['destination']: item for item in policy['files']}
    found = set()
    # /lib is often a relative symlink into /usr/lib in merged-/usr systems.
    for prefix in ('lib/firmware', 'usr/lib/firmware'):
        folder = root / prefix
        if not folder.exists():
            continue
        if not folder.resolve().is_relative_to(root):
            errors.append('固件目录越过镜像边界：' + prefix)
            continue
        for base, directories, files in os.walk(folder, followlinks=False, onerror=unreadable):
            for name in files + [name for name in directories if (Path(base) / name).is_symlink()]:
                path = Path(base) / name
                destination = '/lib/firmware/' + path.relative_to(folder).as_posix()
                found.add(destination)
                if path.is_symlink() or destination not in allowed:
                    errors.append('未审核的外设固件：' + destination)
                elif file_hash(path) != allowed[destination]['sha256']:
                    errors.append('外设固件 SHA256 不匹配：' + destination)
    errors += ['缺少声明的外设固件：' + name for name in sorted(set(allowed) - found)]
    firmware_roots = [p.resolve() for p in (root / 'lib/firmware', root / 'usr/lib/firmware') if p.exists()]
    for base, directories, files in os.walk(root, followlinks=False, onerror=unreadable):
        relative = Path(base).relative_to(root)
        if not relative.parts:
            directories[:] = [name for name in directories if name not in ('proc', 'sys', 'dev', 'run')]
        if build_overlay and relative.as_posix() == 'tmp':
            directories[:] = [name for name in directories if name != 'overlay']
        for name in files:
            path = Path(base) / name
            if any(path.parent.resolve().is_relative_to(folder) for folder in firmware_roots):
                continue
            lowered = name.lower()
            while Path(lowered).suffix in ('.gz', '.xz', '.zst', '.bz2', '.lz4', '.zip', '.lzma'):
                lowered = str(Path(lowered).with_suffix(''))
            rel = path.relative_to(root).as_posix()
            if (Path(lowered).suffix in ('.bin', '.itb', '.img', '.elf')
                    or re.search(r'(?i)(miniloader|idbloader|u-boot|uboot|rkbin|^bl3[123](?:\.|$))', lowered)) and not runtime_binary_data(path, rel, package_names):
                errors.append('引导链或磁盘载荷进入根文件系统：/' + rel)
            if (lowered in ('k3s', 'k3s-arm64', 'k3s-armhf') or lowered.startswith('k3s-airgap-images')
                    or (lowered == 'install.sh' and 'k3s' in rel.lower())
                    or rel.startswith(('usr/src/k3s/', 'var/lib/rancher/k3s/'))):
                errors.append('上游 K3s 载荷进入出厂镜像：/' + rel)
    for item in inventory:
        if re.match(r'^(armbian-firmware|linux-firmware|firmware-|linux-u-boot|u-boot|rkbin|k3s(?:$|-))', item['package']):
            errors.append('禁止的安装包：' + item['package'])
    return {'schema_version': 1, 'passed': not errors, 'errors': sorted(set(errors)),
            'packages': inventory, 'firmware': sorted(found)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='只读检查镜像根文件系统中的载荷和安装包。')
    parser.add_argument('root', type=Path)
    parser.add_argument('--policy', required=True, type=Path)
    parser.add_argument('--skip-build-overlay', action='store_true', help='仅构建 chroot 使用；发布检查不跳过 /tmp/overlay。')
    args = parser.parse_args(argv)
    try:
        result = audit_rootfs(args.root, json.loads(args.policy.read_text(encoding='utf-8')),
                              build_overlay=args.skip_build_overlay)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['passed'] else 1
    except (OSError, ValueError, KeyError) as exc:
        print('镜像检查失败：' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
