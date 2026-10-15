# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Load and validate framework configuration without external dependencies."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import urllib.parse


ARCHITECTURES = {
    'arm64': {
        'debian_arch': 'arm64', 'kernel_arch': 'arm64', 'bits': 64,
        'kernel_image': 'Image', 'dts_directory': 'arch/arm64/boot/dts',
        'runtime_machines': ['aarch64', 'arm64'], 'k3s_arch': 'arm64',
        'k3s_asset': 'k3s-arm64',
    },
    'armhf': {
        'debian_arch': 'armhf', 'kernel_arch': 'arm', 'bits': 32,
        'kernel_image': 'zImage', 'dts_directory': 'arch/arm/boot/dts',
        'runtime_machines': ['armv7l', 'armv8l'], 'k3s_arch': 'arm',
        'k3s_asset': 'k3s-armhf',
    },
}
CATALOGS = {
    'backend': 'backends', 'soc': 'adapters/soc',
    'board': 'adapters/boards', 'os': 'profiles/os',
    'storage': 'profiles/storage', 'workload': 'profiles/workloads',
    'target': 'targets',
}
ID_PATTERN = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*')
STATES = ('draft', 'source_prepared', 'configured', 'built', 'hardware_verified')
BOOT_CHAIN_POLICY = {
    'provider': 'user', 'scope': 'complete-board-chain',
    'build_in_framework': False, 'include_in_image': False,
    'write_by_framework': False, 'preserve_existing_regions': True,
}
ACCESS_POLICY = {
    'provision_stage': 'local_install', 'create_administrator': True,
    'authentication': 'ssh_public_key', 'root_ssh_login': False,
    'password_ssh_login': False, 'administrator_sudo': 'passwordless',
    'require_provisioning_before_ssh': True,
}
FIRMWARE_POLICY = {'install_armbian_bundle': False, 'selection': 'board_allowlist'}


class ConfigError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ConfigError(message)


def workspace_path(root, relative, *, exists=True):
    """Reject traversal, absolute paths, and symlinks that leave the workspace."""
    require(isinstance(relative, str) and relative, '资源路径不能为空。')
    path = PurePosixPath(relative)
    require('\\' not in relative and ':' not in relative and not path.is_absolute()
            and '..' not in path.parts, f'资源必须使用工作区内的相对路径：{relative}')
    result = (root / relative).resolve()
    require(result.is_relative_to(root.resolve()), f'资源越过工作区边界：{relative}')
    if exists:
        require(result.exists(), f'资源不存在：{relative}')
    return result


def linux_path(value, *, allow_root=False):
    require(isinstance(value, str) and value.startswith('/') and '\\' not in value
            and not any(c.isspace() for c in value), f'无效 Linux 路径：{value}')
    path = PurePosixPath(value)
    require('..' not in path.parts and str(path) == value
            and (allow_root or value != '/'), f'路径必须规范化：{value}')
    return value


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_record(root, kind, identifier):
    require(kind in CATALOGS, f'未知配置类型：{kind}')
    require(isinstance(identifier, str) and ID_PATTERN.fullmatch(identifier),
            f'无效配置标识：{identifier}')
    relative = f'{CATALOGS[kind]}/{identifier}.json'
    path = workspace_path(root, relative)
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ConfigError(f'不能读取 {relative}：{exc}') from exc
    require(isinstance(record, dict), f'{relative} 必须是 JSON 对象。')
    require(record.get('schema_version') == 1, f'{relative} 的 schema_version 应为 1。')
    require(record.get('kind') == kind and record.get('id') == identifier,
            f'{relative} 的 kind/id 与文件位置不一致。')
    if 'status' in record:
        require(record['status'] in STATES, f'{relative} 的状态无效。')
    return record


def merge(base, override):
    """Objects merge recursively; lists and scalar values replace the original."""
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def catalog(root):
    result = {}
    for kind, directory in CATALOGS.items():
        records = []
        for path in sorted((root / directory).glob('*.json')):
            record = load_record(root, kind, path.stem)
            records.append({key: record[key] for key in ('id', 'name', 'status', 'architectures') if key in record})
        result[kind] = records
    return result


def validate_storage(storage, board):
    require(storage['boot_media'] in board['storage_interfaces'], '板卡缺少要求的启动介质。')
    require(storage['strategy'] in ('split-boot-external', 'single-media'), '未实现此存储布局类型。')
    if storage['strategy'] == 'split-boot-external':
        require(storage['external_interface'] in board['storage_interfaces'], '板卡缺少要求的外置存储接口。')
        require(storage['external_interface'] != storage['boot_media'], '启动和外置系统介质必须独立。')
        require(storage['rescue'] is not None, '分离启动与外置系统的方案需要独立 Rescue。')
    else:
        require(storage['external_interface'] is None, '单盘布局不能指定外置系统介质。')
        require(storage['rescue'] is None, '当前单盘配置使用外部救援环境。')
    require(storage['partition_table'] == 'gpt' or
            (storage['partition_table'] == 'mbr' and board.get('soc') == 'amlogic-s922x'
             and board.get('boot',{}).get('partition_table') == 'mbr'), '分区表策略与板卡不匹配。')
    alignment = storage['alignment_bytes']
    size = storage['root']['size_bytes']
    require(isinstance(alignment, int) and alignment >= 1048576
            and alignment & (alignment - 1) == 0, '分区对齐必须是至少 1 MiB 的 2 次幂。')
    require(isinstance(size, int) and size >= 1024**3 and size % alignment == 0,
            '根分区大小必须至少 1 GiB，并按分区对齐单位对齐。')
    labels = []
    for role in ('boot', 'rescue', 'root', 'data', 'rescue_b', 'shared'):
        if role not in storage: continue
        partition = storage[role]
        if role == 'rescue' and partition is None:
            continue
        label = partition['label']
        require(isinstance(label, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,16}', label),
                f'{role} 的文件系统标签应为 1 至 16 个字母、数字、下划线或连字符。')
        filesystem = partition['filesystem']
        require(filesystem in ('ext4', 'xfs', 'vfat') or (filesystem == 'squashfs' and role in ('rescue', 'rescue_b')), f'未识别的文件系统：{filesystem}')
        if storage.get('filesystem_policy') == 'squashfs-rescue' and role not in ('rescue', 'rescue_b'):
            require(filesystem == 'ext4', '所选存储配置要求系统、BOOT、共享区和 DATA 全部使用 ext4。')
        if storage.get('filesystem_policy') == 'all-ext4':
            require(filesystem == 'ext4', '所选存储配置要求全部使用 ext4。')
        if storage.get('filesystem_policy') == 'amlogic-squashfs-rescue':
            expected='vfat' if role=='boot' else 'squashfs' if role in ('rescue','rescue_b') else 'ext4'
            require(filesystem == expected, 'Amlogic 存储角色与文件系统不匹配。')
        if filesystem == 'vfat':
            require(re.fullmatch(r'[A-Z0-9_]{1,11}', label), 'vfat 标签必须为至多 11 个大写字母、数字或下划线。')
        labels.append(label)
    require(len(set(labels)) == len(labels), '同一目标的文件系统标签不能重复。')
    require(storage['root']['mountpoint'] == '/', '根分区挂载点必须为 /。')
    linux_path(storage['boot']['mountpoint'])
    linux_path(storage['data']['mountpoint'])
    require(storage['data']['mountpoint'] not in ('/boot', '/etc', '/usr', '/var', '/run', '/proc', '/sys', '/dev'),
            '数据分区不能覆盖系统目录。')
    for field in ('require_matching_boot_and_modules', 'default_preserve_data_partition'):
        require(storage['recovery'][field] is True, f'恢复策略必须保留安全约束：{field}')


def policy_for(resolved):
    """Derive workload paths from a target, without board names in runtime code."""
    workload = resolved['workload']
    if workload['component'] != 'k3s':
        return None
    settings = workload['settings']
    mount = resolved['storage']['data']['mountpoint']
    config = mount + '/node-config/k3s'
    return {
        'schema_version': 1, 'architecture': resolved['architecture'],
        'runtime_machines': resolved['architecture_info']['runtime_machines'],
        'installer_arch': resolved['architecture_info']['k3s_arch'],
        'display_name': resolved['board']['name'],
        **settings,
        'root_filesystem': resolved['storage']['root']['filesystem'],
        'root_size_bytes': resolved['storage']['root']['size_bytes'],
        'data_mountpoint': mount, 'data_filesystem': resolved['storage']['data']['filesystem'],
        'data_label': resolved['storage']['data']['label'],
        'data_dir': mount + '/k3s', 'configuration_dir': config,
        'state_file': config + '/setup-state.json',
        'completion_marker': '/var/lib/image-framework/k3s/setup-complete',
        'kubelet_source': mount + '/kubelet', 'kubelet_target': '/var/lib/kubelet',
        'node_identity_source': config + '/node', 'node_identity_target': '/etc/rancher/node',
        'local_pv_dir': mount + '/pv', 'logs_dir': mount + '/log',
        'backups_dir': mount + '/backups', 'required_packages': workload['packages'],
    }


def https_url(value):
    require(isinstance(value, str) and not any(c.isspace() for c in value),
            '下载地址不能包含空白字符。')
    url = urllib.parse.urlsplit(value)
    require(url.scheme == 'https' and url.hostname and url.username is None
            and url.password is None and not url.query and not url.fragment,
            '下载地址必须是无凭据、无查询参数的 HTTPS 地址。')
    try:
        port = url.port
    except ValueError as exc:
        raise ConfigError('下载地址端口无效。') from exc
    require(port is None or 1 <= port <= 65535, '下载地址端口无效。')


def validate_source_only(path):
    """Reject boot and K3s payloads in active inputs, regardless of gitignore."""
    name = path.name.lower()
    # Treat compressed payloads exactly like their uncompressed originals.
    compression_suffixes = ('.gz', '.xz', '.zst', '.bz2', '.lz4', '.zip', '.lzma')
    while PurePosixPath(name).suffix in compression_suffixes:
        name = str(PurePosixPath(name).with_suffix(''))
    require(PurePosixPath(name).suffix not in ('.bin', '.img', '.itb', '.elf', '.dtb', '.deb')
            and not re.search(r'\.(?:img|bin|itb|elf|dtb|deb)(?:\.|$)', path.name.lower())
            and not any(part.lower() in ('bootloader', 'rkbin') for part in path.parts)
            and name not in ('k3s', 'k3s-arm64', 'k3s-armhf')
            and not name.startswith('k3s-airgap-images')
            and not (name == 'install.sh' and 'k3s' in '/'.join(path.parts).lower()),
            f'框架构建输入不能包含引导链或上游 K3s 载荷：{path}')


def validate_acquisition(root, item):
    """Each target declares its sources; no board-specific downloader defaults."""
    acquisition = item.get('acquire', {'type': 'repository'})
    require(isinstance(acquisition, dict), 'acquire 必须是对象。')
    kind = acquisition.get('type')
    fields = {'repository': {'type'}, 'https': {'type', 'url'},
              'git-archive': {'type', 'url', 'commit', 'git_dir', 'prefix'}}
    require(kind in fields and set(acquisition) == fields[kind], '源码获取方式或字段无效。')
    if kind != 'repository':
        require(item['path'].startswith('cache/sources/'), '下载文件必须放在 cache/sources 内。')
        https_url(acquisition['url'])
    if kind == 'git-archive':
        require(re.fullmatch(r'[0-9a-f]{40}', acquisition['commit']), 'git-archive 必须固定完整 commit。')
        require(acquisition['git_dir'].startswith('sources/'), '上游 Git 缓存必须放在 sources 内。')
        workspace_path(root, acquisition['git_dir'], exists=False)
        require(re.fullmatch(r'[A-Za-z0-9._-]+/', acquisition['prefix']), 'git archive prefix 无效。')
    for relative in item.get('reuse_paths', []):
        require(relative.startswith('cache/sources/'), '复用路径必须位于源码缓存。')
        workspace_path(root, relative, exists=False)
        validate_source_only(PurePosixPath(relative))
    return acquisition


def validate_firmware(root, board, *, verify_sources, require_sources):
    """Peripheral blobs need individual provenance and redistribution approval."""
    result = []
    destinations = set()
    for entry in board.get('firmware', []):
        item = deepcopy(entry)
        fields = {'id', 'path', 'sha256', 'destination', 'license', 'license_file',
                  'redistributable', 'reviewed', 'acquire'}
        require(set(item) == fields, '板卡固件清单字段无效。')
        require(item['reviewed'] is True and item['redistributable'] is True,
                '板卡固件必须逐项审核许可证和再分发条件。')
        require(isinstance(item['license'], str) and item['license'], '板卡固件缺少许可证。')
        license_path = workspace_path(root, item['license_file'])
        require(license_path.is_file() and item['license_file'].startswith('adapters/boards/'),
                '板卡固件许可文本必须保存在板卡源码目录。')
        require(item['path'].startswith('cache/firmware/'), '外设固件只缓存在 cache/firmware。')
        path = workspace_path(root, item['path'], exists=require_sources or verify_sources)
        linux_path(item['destination'])
        require(item['destination'].startswith('/lib/firmware/') and item['destination'] not in destinations,
                '固件目标必须唯一且位于 /lib/firmware。')
        # A peripheral allowlist must never admit the user-supplied early boot chain.
        for name in (item['path'], item['destination']):
            require(not re.search(r'(?i)(miniloader|idbloader|u-boot|uboot|rkbin|bl31|bl32|bl33|trust\.img|ddr|spl|tpl|tee)', name),
                    '外设固件清单不能包含早期引导链。')
        require(re.fullmatch(r'[0-9a-f]{64}', item['sha256']), '固件缺少 SHA256。')
        require(set(item['acquire']) == {'type', 'url'} and item['acquire']['type'] == 'https',
                '外设固件需要明确 HTTPS 来源。')
        https_url(item['acquire']['url'])
        if path.exists():
            require(path.is_file(), '固件输入必须是文件。')
        if verify_sources:
            require(sha256(path) == item['sha256'], f'固件校验失败：{item["path"]}')
        destinations.add(item['destination'])
        item.update(owner='board/' + board['id'], present=path.is_file(), verified=verify_sources)
        result.append(item)
    return result


def resolve(root, identifier, *, verify_sources=False, require_sources=True):
    root = Path(root).resolve()
    target = load_record(root, 'target', identifier)
    allowed = {'schema_version', 'kind', 'id', 'name', 'status', 'architecture', 'backend',
               'board', 'os', 'storage', 'workload', 'overrides', 'description'}
    require(not set(target) - allowed, f'目标有未知字段：{sorted(set(target) - allowed)}')
    board = load_record(root, 'board', target['board'])
    soc = load_record(root, 'soc', board['soc'])
    records = {'backend': load_record(root, 'backend', target['backend']), 'board': board, 'soc': soc}
    for kind in ('os', 'storage', 'workload'):
        records[kind] = load_record(root, kind, target[kind])
    arch = target['architecture']
    require(arch in ARCHITECTURES, f'当前框架支持 arm64 和 armhf：{arch}')
    for kind in ('backend', 'soc', 'board', 'os', 'workload'):
        require(arch in records[kind]['architectures'], f'{kind}/{records[kind]["id"]} 不支持 {arch}。')
    require(records['backend']['engine'] == 'armbian', '首阶段只实现 Armbian 后端。')
    require(records['backend']['id'] in soc['backend_families'], 'SoC 没有此构建后端的 family 映射。')
    kernel = soc['kernel']
    require(re.fullmatch(r'[0-9a-f]{40}', kernel['commit']), '芯片内核来源必须固定完整 commit。')
    require(re.fullmatch(r'\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9._-]+)?', kernel['version']),
            '芯片内核版本格式无效。')
    source_url = urllib.parse.urlsplit(kernel['source'])
    require(source_url.scheme == 'https' and source_url.hostname
            and source_url.username is None and source_url.password is None, '内核源码需要无凭据的 HTTPS 地址。')
    overrides = target.get('overrides', {})
    require(isinstance(overrides, dict) and not set(overrides) - {'storage'},
            '目标只允许覆写存储参数；应用设置通过独立 workload 配置选择。')
    if 'storage' in overrides:
        allowed_overrides = {'boot_media', 'external_interface', 'boot', 'rescue', 'root', 'data'}
        require(not set(overrides['storage']) - allowed_overrides, '目标存储覆写包含未知字段。')
        for role in ('boot', 'rescue', 'root', 'data'):
            if role in overrides['storage']:
                fields = {'label', 'mountpoint'} | ({'size_bytes'} if role == 'root' else set())
                require(not set(overrides['storage'][role]) - fields, f'{role} 存储覆写包含未知字段。')
        records['storage'] = merge(records['storage'], overrides['storage'])
    validate_storage(records['storage'], board)
    workload = records['workload']
    require(workload['component'] in ('none', 'k3s'), '未实现此应用组件。')
    from .boot_chain import validate_boot_chain
    permanent_boot, boot_blockers = validate_boot_chain(root, board, verify_sources=verify_sources,
                                                      require_sources=require_sources)
    require(records['os'].get('access_policy') == ACCESS_POLICY,
            '管理员和 SSH 公钥必须在本地安装阶段配置，并在开放 SSH 前完成。')
    require(records['os'].get('firmware_policy') == FIRMWARE_POLICY,
            '固件策略必须关闭 Armbian 整包安装，并使用板卡明确清单。')
    workspace_path(root, board['boot']['dts'])
    require(any(item['path'] == board['boot']['dts'] for item in board['inputs']),
            '板卡 DTS 必须登记为带 SHA256 的构建输入。')
    dtb = PurePosixPath(board['boot']['dtb'])
    require(not dtb.is_absolute() and '..' not in dtb.parts and '\\' not in str(dtb)
            and ':' not in str(dtb) and str(dtb).endswith('.dtb'), '板卡 DTB 路径无效。')
    components = ['common'] + ([workload['component']] if workload['component'] != 'none' else [])
    for component in components:
        workspace_path(root, f'components/{component}/rootfs-overlay')
    board_overlay = board.get('workload_overlays', {}).get(workload['component'])
    if board_overlay:
        workspace_path(root, board_overlay)
    for package in records['os']['packages'] + workload['packages']:
        require(isinstance(package, str) and re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', package),
                f'无效软件包名称：{package}')
        require(not re.match(r'^(armbian-firmware|linux-firmware|firmware-)', package),
                '固件包必须通过板卡逐文件审核清单提供。')
    requirements = {}
    for kind in ('backend', 'soc', 'board', 'os', 'storage', 'workload'):
        for symbol, allowed_values in records[kind].get('kernel_requirements', {}).items():
            require(re.fullmatch(r'CONFIG_[A-Z0-9_]+', symbol)
                    and isinstance(allowed_values, list) and allowed_values
                    and set(allowed_values) <= {'y', 'm'}, f'无效内核选项：{symbol}')
            values = sorted(set(requirements.get(symbol, allowed_values)) & set(allowed_values))
            require(values, f'内核要求冲突：{symbol}')
            requirements[symbol] = values
    inputs = []
    staged_input_names = set()
    for kind in ('backend', 'soc', 'board', 'os', 'storage', 'workload'):
        for entry in records[kind].get('inputs', []):
            item = deepcopy(entry)
            path = workspace_path(root, item['path'], exists=require_sources or verify_sources)
            validate_source_only(path.relative_to(root))
            if path.exists():
                require(path.is_file(), f'输入必须是文件：{item["path"]}')
            require(re.fullmatch(r'[0-9a-f]{64}', item['sha256']), f'输入缺少 SHA256：{item["path"]}')
            item['acquire'] = validate_acquisition(root, item)
            if 'stage_as' in item:
                require(isinstance(item['stage_as'], str) and item['stage_as']
                        and not PurePosixPath(item['stage_as']).is_absolute(), 'stage_as 必须是相对路径。')
                workspace_path(root, 'build/board-inputs/' + item['stage_as'], exists=False)
                validate_source_only(PurePosixPath(item['stage_as']))
                require(item['stage_as'] not in staged_input_names, f'构建输入目标重名：{item["stage_as"]}')
                staged_input_names.add(item['stage_as'])
            if verify_sources:
                require(sha256(path) == item['sha256'], f'输入校验失败：{item["path"]}')
            item.update({'owner': kind + '/' + records[kind]['id'], 'size_bytes': path.stat().st_size if path.is_file() else None,
                         'present': path.is_file(),
                         'verified': verify_sources})
            inputs.append(item)
    firmware = validate_firmware(root, board, verify_sources=verify_sources, require_sources=require_sources)
    archive = [item for item in inputs if item['owner'].startswith('backend/') and item['id'] == 'build-framework']
    require(len(archive) == 1 and archive[0]['acquire']['type'] == 'git-archive'
            and archive[0]['acquire']['commit'] == records['backend']['source']['commit']
            and archive[0]['acquire']['url'] == records['backend']['source']['url'],
            'Armbian 快照获取描述必须与 backend 固定源码一致。')
    backend_files = board.get('backend_files', {}).get(records['backend']['id'], [])
    for item in backend_files:
        source = workspace_path(root, item['source'])
        validate_source_only(source.relative_to(root))
        destination = PurePosixPath(item['destination'])
        require(not destination.is_absolute() and '..' not in destination.parts
                and '\\' not in item['destination'] and ':' not in item['destination'],
                '后端目标文件必须留在 userpatches 内。')
        require(destination.parts and destination.parts[0] in ('config', 'kernel', 'bootscripts', 'bootenv'),
                '板卡后端文件只能写入受支持的 userpatches 子目录。')
        validate_source_only(destination)
    blockers = []
    validation_todo = []
    for kind, record in records.items():
        for reason in record.get('blockers', []):
            blockers.append({'owner': kind + '/' + record['id'], 'reason': reason})
        for reason in record.get('validation_todo', []):
            validation_todo.append({'owner': kind + '/' + record['id'], 'reason': reason})
    blockers += [{'owner': 'board/' + board['id'], 'reason': reason} for reason in boot_blockers]
    boot_inputs = permanent_boot + [source for item in permanent_boot for source in item['corresponding_sources']]
    if not board.get('armbian_board') or not backend_files:
        blockers.append({'owner': 'board/' + board['id'], 'reason': '尚未提供 Armbian 板卡定义及板卡构建文件。'})
    resolved = {
        'schema_version': 1, 'target': target, 'architecture': arch,
        'architecture_info': deepcopy(ARCHITECTURES[arch]), **records,
        'components': components,
        'packages': sorted(set(records['os']['packages'] + workload['packages'])),
        'kernel_requirements': dict(sorted(requirements.items())), 'inputs': inputs,
        'firmware': firmware, 'firmware_policy': deepcopy(FIRMWARE_POLICY),
        'permanent_boot': permanent_boot,
        'missing_inputs': [item['path'] for item in inputs + firmware + boot_inputs if not item['present']],
        'backend_files': backend_files, 'board_overlay': board_overlay,
        'build_ready': not blockers and all(item['present'] for item in inputs + firmware + boot_inputs),
        'blockers': blockers, 'validation_todo': validation_todo,
    }
    policy = policy_for(resolved)
    if policy:
        setting_fields = {'deployment_mode', 'default_version', 'installer_url', 'installer_sha256',
                          'include_k3s_binary_in_image', 'include_airgap_archive_in_image',
                          'include_installer_in_image', 'download_source_selection', 'artifact_url',
                          'auto_install_at_boot', 'service_start_timeout_seconds', 'cgroup_mode', 'swap_enabled'}
        require(set(workload['settings']) == setting_fields, 'K3s settings 存在未知字段或缺少必要字段。')
        require(re.fullmatch(r'v\d+\.\d+\.\d+\+k3s\d+', policy['default_version']), 'K3s 版本必须完整固定。')
        require(re.fullmatch(r'[0-9a-f]{64}', policy['installer_sha256']), 'K3s 安装脚本必须固定 SHA256。')
        https_url(policy['installer_url'])
        https_url(policy['artifact_url'])
        require(policy['deployment_mode'] == 'ssh_wizard_online' and not policy['auto_install_at_boot']
                and not policy['include_k3s_binary_in_image'] and not policy['include_airgap_archive_in_image']
                and policy['include_installer_in_image'] is False
                and policy['download_source_selection'] == 'ssh_select',
                '当前 K3s 组件使用 SSH 交互引导及在线下载。')
        require(policy['cgroup_mode'] == 'v2' and policy['swap_enabled'] is False,
                '当前 K3s 组件要求 cgroup v2 和关闭 swap。')
        require(not workload.get('inputs'), '上游 K3s 文件只在首次 SSH 引导中下载，不作为镜像构建输入。')
    resolved['setup_policy'] = policy
    return resolved
