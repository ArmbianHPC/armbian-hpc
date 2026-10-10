# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Release gates and local bundle preparation. This module never publishes a release."""

import gzip
import hashlib
import json
import lzma
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import zlib

from .config import ConfigError, require, resolve, sha256, validate_source_only, workspace_path
from .image_audit import audit_rootfs
from .sources import git_command
from .staging import write_json, write_text


def source_audit(root, reference='HEAD'):
    root = Path(root).resolve()
    require(reference and not reference.startswith('-'), '源码 ref 无效。')
    commit = git_command(root, 'rev-parse', '--verify', reference + '^{commit}', stdout=subprocess.PIPE).stdout.decode().strip()
    tree = git_command(root, 'ls-tree', '-rz', commit, stdout=subprocess.PIPE).stdout
    entries = {}
    errors = []
    for row in tree.split(b'\0'):
        if not row:
            continue
        meta, name = row.split(b'\t', 1)
        mode, kind, blob = meta.decode().split()
        relative = name.decode('utf-8')
        entries[relative] = blob
        try:
            path = PurePosixPath(relative)
            require(mode in ('100644', '100755') and kind == 'blob', '源码导出不接受链接或子模块。')
            require(path.parts[0] not in ('build', 'local', 'private', '.git', '.aws', '.codex', '.agents'), '私有目录或构建输出进入源码。')
            if path.parts[0] in ('cache', 'sources'):
                require(relative in ('cache/README.md', 'sources/README.md'), '上游缓存进入主仓库。')
            require(path.suffix.lower() not in ('.pem', '.key', '.pub')
                    and path.name not in ('authorized_keys', 'id_rsa', 'id_ed25519'), '设备身份文件进入源码。')
            validate_source_only(path)
            content = git_command(root, 'cat-file', 'blob', blob, stdout=subprocess.PIPE).stdout
            require(not content.startswith((b'\x7fELF', b'version https://git-lfs.github.com/spec/')),
                    '可执行二进制或 Git LFS 指针进入源码。')
            require(not re.search(rb'(?m)^-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----\r?\n[A-Za-z0-9+/]{40,}', content),
                    '私钥进入源码。')
        except ConfigError as exc:
            errors.append(relative + ': ' + str(exc))
    required = ['LICENSE', 'THIRD_PARTY_NOTICES.md', '.gitignore', 'README.md']
    required += [p.relative_to(root).as_posix() for p in (root/'LICENSES').glob('*.txt')]
    required += [p.name for p in (root/'VERSION',root/'COPYRIGHT',root/'REUSE.toml') if p.is_file()]
    for directory in ('framework', 'scripts', 'adapters', 'backends', 'profiles', 'targets', 'components', 'templates', 'tests', 'docs', 'tools'):
        required += [p.relative_to(root).as_posix() for p in (root / directory).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts and not p.name.endswith(('.pyc', '.pyo'))]
    missing = sorted(set(required) - set(entries))
    errors += ['对应源码 commit 缺少工作区文件：' + name for name in missing]
    if entries:
        # Git handles checkout line ending conversion. Untracked source files are covered above.
        changed = git_command(root, 'diff', '--name-only', commit, '--', *sorted(entries), stdout=subprocess.PIPE).stdout
        errors += ['工作区与对应源码 commit 不一致：' + name for name in changed.decode('utf-8').splitlines()]
    return {'schema_version': 1, 'source_ref': reference, 'source_commit': commit,
            'passed': not errors, 'errors': errors, 'source_files': len(entries)}


def check_disk_image(path, boot_components=None):
    """Check GPT/ext4; only reviewed permanent payloads may occupy declared gaps."""
    from .boot_chain import PERMANENT_ROLES
    boot_components = boot_components or []
    size = path.stat().st_size
    require(size >= 2048, '镜像太小。')
    with path.open('rb') as stream:
        stream.seek(1080)
        if stream.read(2) == b'\x53\xef':
            require(not boot_components, '永久引导组件需要 GPT 整盘镜像。')
            stream.seek(0)
            require(stream.read(1024) == b'\0' * 1024, 'ext4 镜像的保留引导区存在载荷。')
            return {'type': 'ext4-filesystem', 'partitions': 1, 'partition_sizes_bytes': [size]}
        require(size % 512 == 0, 'GPT 镜像必须按 512 字节扇区对齐。')
        stream.seek(0)
        mbr = stream.read(512)
        require(mbr[:446] == b'\0' * 446 and mbr[510:] == b'\x55\xaa'
                and mbr[450] == 0xee and mbr[462:510] == b'\0' * 48,
                'GPT 镜像包含 MBR 引导代码或不符合保护 MBR 约定。')
        def header(lba):
            stream.seek(lba * 512)
            block = bytearray(stream.read(512))
            require(block[:8] == b'EFI PART', '缺少 GPT 表头。')
            length = struct.unpack_from('<I', block, 12)[0]
            require(92 <= length <= 512, 'GPT 表头长度无效。')
            crc = struct.unpack_from('<I', block, 16)[0]
            struct.pack_into('<I', block, 16, 0)
            require(zlib.crc32(block[:length]) == crc, 'GPT 表头 CRC 不匹配。')
            require(not any(block[length:]) and block[20:24] == b'\0' * 4,
                    'GPT 表头保留区域存在额外载荷。')
            current, other, first, last = struct.unpack_from('<QQQQ', block, 24)
            entries_lba, count, entry_size, entries_crc = struct.unpack_from('<QIII', block, 72)
            require(current == lba and 128 <= entry_size <= 4096 and 1 <= count <= 4096,
                    'GPT 表头或分区表规模无效。')
            require(entries_lba * 512 + count * entry_size <= size, 'GPT 分区表越界。')
            stream.seek(entries_lba * 512)
            entries = stream.read(count * entry_size)
            require(zlib.crc32(entries) == entries_crc, 'GPT 分区表 CRC 不匹配。')
            return other, first, last, entries_lba, entries
        backup_lba, first, last, table_lba, entries = header(1)
        require(backup_lba == size // 512 - 1 and 2 <= first <= last < backup_lba, 'GPT 备份或可用范围无效。')
        other, bfirst, blast, backup_table, backup_entries = header(backup_lba)
        require(other == 1 and (first, last, entries) == (bfirst, blast, backup_entries), '主、备 GPT 不一致。')
        # Read this field directly; never load a large disk image into memory.
        stream.seek(512 + 84)
        entry_size = struct.unpack('<I', stream.read(4))[0]
        regions = [(0, 1024), (table_lba * 512, table_lba * 512 + len(entries)),
                   (backup_table * 512, backup_table * 512 + len(entries)), (backup_lba * 512, size)]
        partitions = []
        for offset in range(0, len(entries), entry_size):
            entry = entries[offset:offset + entry_size]
            if entry[:16] == b'\0' * 16:
                require(entry == b'\0' * entry_size, '未使用 GPT 条目包含额外载荷。')
                continue
            start, end = struct.unpack_from('<QQ', entry, 32)
            require(first <= start <= end <= last, 'GPT 分区越界。')
            require((end - start + 1) * 512 >= 2048, 'ext4 分区长度无效。')
            stream.seek(start * 512 + 1080)
            require(stream.read(2) == b'\x53\xef', '镜像所有分区必须使用 ext4。')
            stream.seek(start * 512)
            require(stream.read(1024) == b'\0' * 1024, '分区的 ext4 保留引导区存在载荷。')
            partitions.append((offset // entry_size + 1, start * 512, (end + 1) * 512))
        require(partitions, 'GPT 镜像没有分区。')
        regions += [(start, end) for number, start, end in partitions]
        boot_inventory = []
        first_partition = min(start for number, start, end in partitions)
        for component in boot_components:
            require(component.get('reviewed') is True and component.get('redistributable') is True
                    and component.get('verified') is True and component.get('license')
                    and component.get('role') in PERMANENT_ROLES,
                    '永久引导组件缺少来源、许可或 SHA256 校验。')
            require('miniloader' not in (component['id'] + component['path']).lower(),
                    'MiniLoader 不能进入发布镜像。')
            start = component['offset_bytes']
            length = component['size_bytes']
            end = start + length
            require(type(start) is int and start % 512 == 0 and length > 0
                    and 0 < start < end <= first_partition, '永久引导组件越过原始引导区。')
            require(all(end <= a or start >= b for a, b in regions),
                    '永久引导组件与 GPT 元数据、文件系统或其他组件重叠。')
            stream.seek(start)
            digest = hashlib.sha256()
            remaining = length
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                require(chunk, '永久引导组件读取不完整。')
                digest.update(chunk)
                remaining -= len(chunk)
            require(digest.hexdigest() == component['sha256'], '镜像中的永久引导组件 SHA256 不匹配。')
            regions.append((start, end))
            boot_inventory.append({key: component[key] for key in ('id', 'role', 'offset_bytes', 'size_bytes', 'sha256', 'license')})
        cursor = 0
        for start, end in sorted(regions):
            require(start >= cursor, 'GPT 元数据或分区重叠。')
            stream.seek(cursor)
            remaining = start - cursor
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                require(chunk and not any(chunk), '镜像分区外存在载荷；可能包含用户引导链。')
                remaining -= len(chunk)
            cursor = end
        return {'type': 'gpt-ext4', 'partitions': len(partitions), 'raw_gaps_zero': not boot_components,
                'unlisted_raw_gaps_zero': True, 'permanent_boot': boot_inventory,
                'partition_sizes_bytes': {str(number): end - start for number, start, end in partitions}}


def verify_mount(image, directory):
    require(sys.platform.startswith('linux'), '实际镜像检查需在 Linux 上只读挂载镜像；Windows 只能检查源码。')
    mount = Path(directory).resolve()
    require(mount != Path('/'), 'Release 检查不能使用正在运行的根文件系统。')
    result = subprocess.run(['findmnt', '--json', '--mountpoint', str(mount), '--output', 'SOURCE,FSTYPE,OPTIONS'],
                            capture_output=True, text=True, check=True)
    filesystems = json.loads(result.stdout)['filesystems']
    require(len(filesystems) == 1, '镜像挂载点无效。')
    filesystem = filesystems[0]
    options = filesystem['options'].split(',')
    require(filesystem['fstype'] == 'ext4' and 'ro' in options
            and any(value in options for value in ('noload', 'norecovery')), 'ext4 检查挂载必须使用 ro,noload。')
    loops = subprocess.run(['losetup', '--list', '--json', '--output', 'NAME,BACK-FILE,RO'],
                           capture_output=True, text=True, check=True)
    for loop in json.loads(loops.stdout)['loopdevices']:
        if re.fullmatch(re.escape(loop['name']) + r'(?:p\d+)?', filesystem['source']):
            require(str(loop['ro']) in ('1', 'True', 'true') and Path(loop['back-file']).resolve() == image.resolve(),
                    '挂载点未关联到本次镜像的只读 loop 设备。')
            # Nested mounts could conceal files in the actual image.
            mounts = subprocess.run(['findmnt', '--json', '--submounts', '--mountpoint', str(mount)],
                                    capture_output=True, text=True, check=True)
            require(not any(item.get('children') for item in json.loads(mounts.stdout)['filesystems']),
                    '检查目录中不能有嵌套挂载。')
            label = subprocess.run(['blkid', '-p', '-s', 'LABEL', '-o', 'value', filesystem['source']],
                                   capture_output=True, text=True, check=True).stdout.strip()
            return {'directory': mount, 'device': filesystem['source'], 'label': label}
    raise ConfigError('镜像根目录必须来自对应镜像的只读 loop 挂载。')


def publication_asset(image, path):
    if path.resolve() == image.resolve():
        return
    opener = lzma.open if path.name.endswith('.img.xz') else gzip.open if path.name.endswith('.img.gz') else None
    require(opener is not None, '检查入口当前支持原始 .img 或 .img.xz/.img.gz 发布附件。')
    digest = hashlib.sha256()
    with opener(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    require(digest.hexdigest() == sha256(image), '压缩发布附件与实际检查的镜像不一致。')


def check_release(root, specification):
    root = Path(root).resolve()
    spec = json.loads(Path(specification).read_text(encoding='utf-8'))
    require(spec['schema_version'] == 1, 'Release schema_version 必须为 1。')
    resolved = resolve(root, spec['target'], verify_sources=True)
    source = source_audit(root, spec['source_ref'])
    errors = list(source['errors']) + [item['reason'] for item in resolved['blockers']]
    require(isinstance(spec['images'], list) and spec['images'], 'Release 未提供实际镜像。')
    if errors:
        return {'schema_version': 1, 'passed': False, 'source': source, 'errors': errors}
    record = json.loads(workspace_path(root, spec['build_record']).read_text(encoding='utf-8'))
    require(record['status'] == 'success' and record['target'] == spec['target']
            and record['source_commit'] == source['source_commit'], '实际构建记录与目标/源码不一致。')
    hardware = json.loads(workspace_path(root, spec['hardware_report']).read_text(encoding='utf-8'))
    require(hardware['passed'] is True and hardware['target'] == spec['target'], '缺少对应目标的实机验收记录。')
    require(set(hardware['completed_checks']) >= {item['reason'] for item in resolved['validation_todo']},
            '实机验收记录未覆盖目标 validation_todo。')
    firmware_policy = {'files': resolved['firmware']}
    inventory = []
    roles = set()
    devices = set()
    images = []
    for item in spec['images']:
        image = workspace_path(root, item['path'])
        require(item['path'].startswith('build/') and image.suffix == '.img', '镜像必须位于本地 build 下且为未压缩 .img。')
        digest = sha256(image)
        require(record['images'].get(item['path']) == digest and hardware['images'].get(item['path']) == digest,
                '构建/实机记录中的镜像 SHA256 不一致。')
        components = resolved['permanent_boot'] if 'boot' in item['mounts'] else []
        geometry = check_disk_image(image, components)
        for role, directory in item['mounts'].items():
            require(role in ('normal', 'rescue', 'boot', 'data') and role not in roles, '镜像挂载角色无效或重复。')
            mounted = verify_mount(image, directory)
            require(mounted['device'] not in devices, '同一文件系统不能重复充当不同 Release 角色。')
            devices.add(mounted['device'])
            partition = resolved['storage']['root' if role == 'normal' else role]
            require(mounted['label'] == partition['label'], '镜像文件系统标签与目标配置不一致。')
            if geometry['type'] == 'gpt-ext4':
                part = re.search(r'p(\d+)$', mounted['device'])
                require(part is not None and part[1] in geometry['partition_sizes_bytes'], '镜像设备分区号无效。')
                partition_size = geometry['partition_sizes_bytes'][part[1]]
            else:
                partition_size = geometry['partition_sizes_bytes'][0]
            if partition.get('size_bytes') is not None:
                require(partition_size == partition['size_bytes'],
                        '镜像分区大小与目标配置不一致：' + role)
            mount = mounted['directory']
            if role in ('normal', 'rescue'):
                audit = audit_rootfs(mount, firmware_policy)
                require(audit['passed'], '镜像载荷检查失败：' + '; '.join(audit['errors']))
                inventory += audit['packages']
            else:
                # Boot/data are not dpkg roots; inspect their filenames for early boot/K3s payloads.
                for file in mount.rglob('*'):
                    if file.is_file():
                        relative = file.relative_to(mount)
                        name = file.name.lower()
                        require(not re.search(r'(miniloader|idbloader|u-boot|uboot|rkbin|bl3[123])', name), 'boot/data 包含用户引导链。')
                        require(file.suffix.lower() not in ('.bin', '.itb', '.img', '.elf')
                                and name not in ('k3s', 'k3s-arm64', 'k3s-armhf')
                                and not name.startswith('k3s-airgap-images'), 'boot/data 包含禁止载荷。')
            roles.add(role)
        require(len(item['mounts']) == geometry['partitions'], '每个镜像分区都必须检查，不能遗漏未挂载分区。')
        require(sha256(image) == digest, '检查过程中镜像发生变化。')
        asset = workspace_path(root, item.get('publication_path', item['path']))
        require(asset.is_relative_to((root / 'build').resolve()), 'Release 镜像附件必须放在 build 内。')
        publication_asset(image, asset)
        images.append({'path': asset.relative_to(root).as_posix(), 'sha256': sha256(asset),
                       'image_sha256': digest, 'geometry': geometry})
    expected_roles = {'normal', 'boot', 'data'} | ({'rescue'} if resolved['storage']['rescue'] else set())
    require(roles == expected_roles, 'Release 需检查 boot、normal、data 及配置要求的 Rescue 文件系统。')
    sources = {}
    assets = list(images)
    for item in spec['corresponding_sources']:
        path = workspace_path(root, item['path'])
        validate_source_only(PurePosixPath(item['path']))
        require(sha256(path) == item['sha256'], '对应源码附件 SHA256 不匹配。')
        require((item['source'], item['version']) not in sources, '对应源码包记录重复。')
        sources[(item['source'], item['version'])] = item
        assets.append({'path': item['path'], 'sha256': item['sha256']})
    boot_sources = [source for item in resolved['permanent_boot'] for source in item['corresponding_sources']]
    for item in boot_sources:
        release_source = sources.get((item['source'], item['version']))
        require(release_source is not None and release_source['sha256'] == item['sha256'],
                '缺少与永久引导组件匹配的完整对应源码附件。')
    for name in sorted({item['license_file'] for item in resolved['permanent_boot']}):
        path = workspace_path(root, name)
        assets.append({'path': name, 'sha256': sha256(path)})
    missing = sorted({(item['source'], item['source_version']) for item in inventory} - set(sources))
    require(not missing, '缺少安装包对应源码附件：' + ', '.join(name + '=' + version for name, version in missing))
    notices = workspace_path(root, spec['notices'])
    require(notices.is_file() and notices.stat().st_size > 0, '缺少镜像实际组件的许可证通知。')
    assets.append({'path': spec['notices'], 'sha256': sha256(notices)})
    require(len({Path(item['path']).name for item in assets}) == len(assets), 'Release 附件文件名重名。')
    return {'schema_version': 1, 'passed': True, 'target': spec['target'], 'architecture': resolved['architecture'],
            'board': resolved['board']['id'], 'kernel': resolved['soc']['kernel'], 'source': source,
            'boot_chain': resolved['board']['boot']['chain'], 'permanent_boot': resolved['permanent_boot'],
            'firmware_policy': resolved['firmware_policy'],
            'packages': [json.loads(item) for item in sorted({json.dumps(item, sort_keys=True) for item in inventory})], 'assets': assets,
            'errors': [], 'note': '技术检查通过；组件许可条件和实机记录真实性需由发布维护者核对。'}


def prepare_release(root, specification, output):
    result = check_release(root, specification)
    require(result['passed'], 'Release 检查未通过：' + '; '.join(result['errors']))
    root = Path(root).resolve()
    destination = workspace_path(root, output, exists=False)
    require(destination.is_relative_to((root / 'build/releases').resolve())
            and destination != (root / 'build/releases').resolve(), '附件输出必须位于 build/releases 独立子目录。')
    require(not destination.exists(), 'Release 附件目录已存在。')
    destination.mkdir(parents=True)
    write_text(destination / '.incomplete', 'Release bundle preparation is incomplete.\n')
    archive = destination / ('armbian-hpc-source-' + result['source']['source_commit'][:12] + '.tar')
    with archive.open('wb') as stream:
        git_command(root, '-c', 'core.autocrlf=false', '-c', 'core.eol=lf',
                    'archive', '--format=tar', '--prefix=armbian-hpc/', result['source']['source_commit'], stdout=stream)
    hashes = {archive.name: sha256(archive)}
    for item in result['assets']:
        source = workspace_path(root, item['path'])
        require(sha256(source) == item['sha256'], '准备附件时输入发生变化。')
        target = destination / source.name
        require(not target.exists(), '附件名称与源码归档重名。')
        shutil.copyfile(source, target)
        require(sha256(target) == item['sha256'], 'Release 附件复制校验失败。')
        hashes[target.name] = item['sha256']
    write_json(destination / 'release-metadata.json', result)
    hashes['release-metadata.json'] = sha256(destination / 'release-metadata.json')
    write_text(destination / 'SHA256SUMS', ''.join(digest + '  ' + name + '\n' for name, digest in sorted(hashes.items())))
    (destination / '.incomplete').unlink()
    return destination


def prepare_source_release(root, reference, output):
    """Export the audited source commit independently of disk-image publication."""
    import io, tarfile
    root=Path(root).resolve();output=Path(output).resolve()
    require(not output.is_relative_to(root) and not any((p/'.git').exists() for p in (output,*output.parents)), 'Source assets must be outside repositories')
    audit=source_audit(root,reference);require(audit['passed'],'Source audit failed: '+'; '.join(audit['errors']))
    number=(root/'VERSION').read_text().strip();require(re.fullmatch(r'\d+\.\d+\.\d+',number),'Invalid source version')
    output.mkdir(parents=True,exist_ok=True);archive=output/('armbian-hpc-'+number+'-source.tar.xz')
    require(not archive.exists(),'Release source already exists')
    raw=git_command(root,'archive','--format=tar','--prefix=armbian-hpc-'+number+'/',audit['source_commit'],stdout=subprocess.PIPE).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as source,tarfile.open(archive,'w:xz') as target:
        for member in source.getmembers():
            require(member.isfile() or member.isdir(),'Unexpected source member');member.uid=member.gid=0;member.uname=member.gname=''
            target.addfile(member,source.extractfile(member) if member.isfile() else None)
    record={'kind':'source','version':number,'source_commit':audit['source_commit'],'source_ref':reference,
            'files':[{'file':archive.name,'bytes':archive.stat().st_size,'sha256':sha256(archive)}]}
    (output/'framework-release.json').write_text(json.dumps(record,indent=2)+'\n')
    return record
