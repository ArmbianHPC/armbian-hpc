# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Materialize reviewable build inputs; this module never accesses a disk device."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil

from .config import ConfigError, require, sha256, validate_source_only, workspace_path


def write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(content)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def copy_overlay(source, destination, owners, owner):
    for path in sorted(source.rglob('*')):
        require(not path.is_symlink() and not (hasattr(path, 'is_junction') and path.is_junction()),
                f'Overlay 不接受符号链接；请通过板卡构建钩子处理：{path}')
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if '__pycache__' in relative.parts or path.suffix == '.pyc':
            continue
        validate_source_only(relative)
        target = destination / relative
        if target.exists():
            require(target.read_bytes() == path.read_bytes(),
                    f'Overlay 内容冲突：{relative.as_posix()}，来源 {owners.get(relative.as_posix())} / {owner}')
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        owners[relative.as_posix()] = owner


def storage_layout(resolved):
    storage = resolved['storage']
    alignment = storage['alignment_bytes']
    result = {
        'schema_version': 1, 'status': 'design_only',
        'target': resolved['target']['id'], 'board': resolved['board']['id'],
        'strategy': storage['strategy'], 'boot_media': storage['boot_media'],
        'boot_disk': {
            'interface': storage['boot_media'],
            'raw_layout_status': resolved['board']['boot']['raw_layout_status'],
            'boot_chain': resolved['board']['boot']['chain'],
            'flash_workflow': resolved['board']['boot'].get('flash_workflow'),
            'partitions': [],
        },
        'recovery': storage['recovery'],
        'note': '此文件供安装器实现及审查使用，当前 stage 不创建分区或磁盘镜像。',
    }
    if storage['strategy'] == 'split-boot-external':
        result['boot_disk']['partitions'] = [dict(role=role, **storage[role]) for role in ('boot', 'rescue')]
        result['external'] = {
            'interface': storage['external_interface'], 'partition_table': storage['partition_table'],
            'alignment_bytes': alignment,
            'partitions': [
                dict(number=1, role='root', start_offset_bytes=alignment, **storage['root']),
                dict(number=2, role='data', start_offset_bytes=alignment + storage['root']['size_bytes'], **storage['data']),
            ],
            'discovery': storage['discovery'],
        }
    else:
        result['boot_disk']['partition_table'] = storage['partition_table']
        result['boot_disk']['alignment_bytes'] = alignment
        result['boot_disk']['partitions'] = [dict(number=index, role=role, **storage[role])
                                            for index, role in enumerate(('boot', 'root', 'data'), 1)]
        result['boot_disk']['first_partition_offset_status'] = 'requires_board_boot_region_layout'
    return result


def customize_script(resolved):
    architecture = resolved['architecture']
    workload = resolved['workload']['component']
    return f'''#!/bin/bash
set -euo pipefail
# Armbian runs this inside the image chroot, never on the build host.
test "$(dpkg --print-architecture)" = {shlex.quote(architecture)}
cp -a /tmp/overlay/. /
python3 - <<'PY'
import json
from pathlib import Path
base = Path('/etc/image-framework')
permissions = json.loads((base / 'permissions.json').read_text())
for name, mode in permissions.items():
    Path(name).chmod(int(mode, 8))
# Debian's ordinary-user PATH includes /usr/local/bin, but not sbin.
command_directory = Path('/usr/local/bin')
command_directory.mkdir(parents=True, exist_ok=True)
for name in permissions:
    if name.startswith('/usr/local/sbin/armhpc'):
        link = command_directory / Path(name).name
        target = '../sbin/' + Path(name).name
        if link.exists() or link.is_symlink():
            if not link.is_symlink() or str(link.readlink()) != target:
                raise SystemExit('Image framework: command alias path conflict: ' + str(link))
        else:
            link.symlink_to(target)
import sys
sys.path.insert(0, '/usr/lib/armhpc')
from command_layout import finalize
finalize(Path('/'), 'normal')
requirements = json.loads((base / 'kernel-requirements.json').read_text())
configs = list(Path('/boot').glob('config-*'))
if not configs:
    raise SystemExit('Image framework: installed kernel config is missing.')
for config in configs:
    values = dict(line.strip().split('=', 1) for line in config.read_text().splitlines() if line.startswith('CONFIG_') and '=' in line)
    missing = [name for name, allowed in requirements.items() if values.get(name) not in allowed]
    if missing:
        raise SystemExit('Image framework: kernel config requirements failed: ' + ', '.join(missing))
if {workload!r} == 'k3s':
    # Leave compression for logs/tmp under the board's existing policy.
    config = Path('/etc/default/armbian-zram-config')
    config.parent.mkdir(parents=True, exist_ok=True)
    original = config.read_text() if config.exists() else ''
    lines = [line for line in original.splitlines() if not line.strip().startswith('SWAP=')]
    config.write_text('\\n'.join(lines + ['SWAP=false']) + '\\n')
    fstab = Path('/etc/fstab')
    lines = fstab.read_text().splitlines()
    fstab.write_text('\\n'.join('# image-framework: ' + line if len(line.split()) >= 3 and not line.lstrip().startswith('#') and line.split()[2] == 'swap' else line for line in lines) + '\\n')
PY
if [[ {shlex.quote(workload)} == k3s ]]; then
    systemctl mask swap.target armbian-swap.service zramswap.service
fi
# Inspect the resulting rootfs, including packages brought in by upstream defaults.
python3 /usr/lib/image-framework/image-audit.py / --policy /etc/image-framework/firmware-policy.json --skip-build-overlay
'''


def kernel_extension(resolved):
    requirements = resolved['kernel_requirements']
    kernel = resolved['soc']['kernel']
    family = resolved['soc']['backend_families'][resolved['backend']['id']]
    major_minor = '.'.join(kernel['version'].split('.')[:2])
    signature = hashlib.sha256(json.dumps(requirements, sort_keys=True).encode()).hexdigest()
    lines = [
        '#!/bin/bash',
        '# Armbian v25.08 post_family_config and custom_kernel_config APIs.',
        'function post_family_config__999_image_framework() {',
        f'    [[ "${{BOARDFAMILY:-}}" == {shlex.quote(family)} ]] || exit_with_error "Board family does not match SoC adapter"',
        f'    declare -g KERNELSOURCE={shlex.quote(kernel["source"])}',
        f'    declare -g KERNELBRANCH={shlex.quote("commit:" + kernel["commit"])}',
        f'    declare -g KERNEL_MAJOR_MINOR={shlex.quote(major_minor)}',
        f'    declare -g BOOT_FDT_FILE={shlex.quote(resolved["board"]["boot"]["dtb"])}',
        '    # Permanent boot payloads use a separate reviewed assembly step.',
        '    declare -g BOOTCONFIG=none ATF_COMPILE=no ATFSOURCE="" INSTALL_ARMBIAN_FIRMWARE=no',
        '    # do_extra_configuration reads a branch-specific BOOTCONFIG after this hook.',
        '    local boot_branch="BOOTCONFIG_${BRANCH^^}"',
        '    boot_branch="${boot_branch//-/_}"',
        '    unset "$boot_branch"',
        '}',
        '',
        'function extension_prepare_config__999_image_framework() {',
        '    # This hook runs after branch defaults and lib.config/user_config.',
        '    post_family_config__999_image_framework',
        '    declare -gr BOOTCONFIG=none ATF_COMPILE=no ATFSOURCE="" INSTALL_ARMBIAN_FIRMWARE=no',
        '}',
        '',
        'function custom_kernel_config__image_framework() {',
        f'    kernel_config_modifying_hashes+=("image-framework={signature}")',
        '    if [[ -f .config ]]; then',
    ]
    for symbol, allowed in requirements.items():
        # Builtins also satisfy requirements that permit modules.
        operation = 'y' if 'y' in allowed else 'm'
        lines.append(f'        kernel_config_set_{operation} {symbol.removeprefix("CONFIG_")}')
    lines += ['    fi', '    return 0', '}', '']
    return '\n'.join(lines)


def rkbin_extension():
    return '''#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
# Armbian v25.08 searches USERPATCHES_PATH/extensions before built-in extensions.
# The complete early boot chain is user supplied. Do not fetch/install rkbin tools.
function fetch_sources_tools__rkbin_tools() { return 0; }
function build_host_tools__install_rkbin_tools() { return 0; }
'''


def backend_inputs(root, destination, resolved, overlay):
    userpatches = destination / 'userpatches'
    shutil.copytree(overlay, userpatches / 'overlay')
    blockers = '\n'.join(f'- {item["owner"]}: {item["reason"]}' for item in resolved['blockers'])
    write_text(destination / 'build-blockers.txt', blockers + ('\n' if blockers else ''))
    write_text(userpatches / 'framework-build-blockers.txt', blockers + ('\n' if blockers else ''))
    board_name = resolved['board'].get('armbian_board') or 'adapter-missing'
    require(board_name == 'adapter-missing' or re.fullmatch(r'[a-z0-9][a-z0-9-]*', board_name),
            'Armbian BOARD 名称无效。')
    assignments = {
        'BOARD': board_name, 'BRANCH': resolved['soc']['kernel']['branch'],
        'RELEASE': resolved['os']['release'],
        'BUILD_DESKTOP': 'yes' if resolved['os']['desktop'] else 'no',
        'BUILD_MINIMAL': 'yes', 'KERNEL_CONFIGURE': 'no',
        'BOOTCONFIG': 'none',
        'ATF_COMPILE': 'no', 'INSTALL_ARMBIAN_FIRMWARE': 'no',
        'PACKAGE_LIST_ADDITIONAL': ' '.join(resolved['packages']),
    }
    lines = [
        '# Generated input for a separate, pinned Armbian checkout.',
        'if [[ -s "${USERPATCHES_PATH}/framework-build-blockers.txt" ]]; then',
        '    cat "${USERPATCHES_PATH}/framework-build-blockers.txt" >&2',
        '    exit_with_error "Image framework adapter is incomplete"',
        'fi',
    ]
    lines += [f'{key}={shlex.quote(value)}' for key, value in assignments.items()]
    lines += ['enable_extension "image-framework"', '']
    write_text(userpatches / 'config-framework.conf', '\n'.join(lines))
    artifact_lines = [
        '# Build kernel/rootfs artifacts while full image assembly remains incomplete.',
        'case "${ARMBIAN_COMMAND:-}" in',
        '    kernel|rootfs) ;;',
        '    *) exit_with_error "This configuration only builds kernel/rootfs artifacts" ;;',
        'esac',
    ]
    artifact_lines += [f'{key}={shlex.quote(value)}' for key, value in assignments.items()]
    artifact_lines += ['enable_extension "image-framework"', '']
    write_text(userpatches / 'config-framework-artifacts.conf', '\n'.join(artifact_lines))
    write_text(userpatches / 'extensions/image-framework.sh', kernel_extension(resolved))
    write_text(userpatches / 'extensions/rkbin-tools.sh', rkbin_extension())
    write_text(userpatches / 'customize-image.sh', customize_script(resolved))
    for item in resolved['backend_files']:
        source = workspace_path(root, item['source'])
        target = userpatches / item['destination']
        require(not target.exists(), f'板卡文件与通用后端输入冲突：{item["destination"]}')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    commit = resolved['backend']['source']['commit']
    require(re.fullmatch(r'[0-9a-f]{40}', commit), '构建后端必须固定完整 commit。')
    write_text(destination / 'build-artifacts.sh', f'''#!/bin/bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")" && pwd)"
artifact="${{1:?Usage: build-artifacts.sh kernel|rootfs PINNED_ARMBIAN_CHECKOUT [KEY=VALUE ...]}}"
checkout="${{2:?Supply a separate Armbian checkout pinned to {commit}}}"
shift 2
case "$artifact" in
    kernel|rootfs) ;;
    *) echo "Only kernel and rootfs artifacts are supported." >&2; exit 2 ;;
esac
test "$(git -C "$checkout" rev-parse HEAD)" = {commit}
test "$(readlink -f "$checkout/userpatches")" = "$here/userpatches"
# Armbian v25.08 uses eval for its compiler PATH. WSL Windows paths contain spaces.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C.UTF-8 COLUMNS=160
cd -- "$checkout"
exec ./compile.sh "$artifact" framework-artifacts \
    SHARE_LOG=no PESTER_TERMINAL=no SKIP_EXTERNAL_TOOLCHAINS=yes COLUMNS=160 "$@"
''')
    write_text(destination / 'build.sh', f'''#!/bin/bash
set -euo pipefail
here="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")" && pwd)"
if [[ -s "$here/build-blockers.txt" ]]; then
    cat "$here/build-blockers.txt" >&2
    exit 78
fi
checkout="${{1:?Supply a separate Armbian Git checkout pinned to {commit}}}"
test "$(git -C "$checkout" rev-parse HEAD)" = {commit}
test "$(readlink -f "$checkout/userpatches")" = "$here/userpatches"
cd -- "$checkout"
exec ./compile.sh framework
''')


def stage(root, resolved, output=None):
    root = Path(root).resolve()
    identifier = resolved['target']['id']
    relative = output or f'build/staging/{identifier}'
    destination = workspace_path(root, relative, exists=False)
    build_root = (root / 'build/staging').resolve()
    require(destination.is_relative_to(build_root) and destination != build_root,
            '输出必须位于工作区 build/staging 的独立子目录内。')
    require(not destination.exists(), f'输出已存在，请选择新输出目录：{relative}')
    # Stage only after every declared source has passed a checksum.
    boot_inputs = resolved['permanent_boot'] + [source for item in resolved['permanent_boot'] for source in item['corresponding_sources']]
    for item in resolved['inputs'] + resolved['firmware'] + boot_inputs:
        source = workspace_path(root, item['path'])
        require(sha256(source) == item['sha256'], f'输入校验失败：{item["path"]}')
        item['verified'] = True
    destination.mkdir(parents=True)
    write_text(destination / '.incomplete', 'Staging has not finished. Do not use these inputs.\n')
    overlay = destination / 'rootfs-overlay'
    owners = {}
    for component in resolved['components']:
        copy_overlay(workspace_path(root, f'components/{component}/rootfs-overlay'), overlay, owners, 'component/' + component)
    if resolved['board_overlay']:
        copy_overlay(workspace_path(root, resolved['board_overlay']), overlay, owners, 'board/' + resolved['board']['id'])
    audit_source = workspace_path(root, 'framework/image_audit.py')
    audit_destination = overlay / 'usr/lib/image-framework/image-audit.py'
    audit_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(audit_source, audit_destination)
    owners['usr/lib/image-framework/image-audit.py'] = 'framework/image-audit'
    for item in resolved['firmware']:
        source = workspace_path(root, item['path'])
        require(sha256(source) == item['sha256'], f'固件校验失败：{item["path"]}')
        # Bookworm uses merged /usr; copying a top-level lib directory would conflict with /lib -> usr/lib.
        staged_name = 'usr/' + item['destination'].lstrip('/')
        target = overlay / staged_name
        require(not target.exists(), '外设固件与 overlay 冲突。')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        owners[staged_name] = item['owner']
    generated = overlay / 'etc/image-framework'
    require(not generated.exists(), 'etc/image-framework 是框架生成配置的保留目录。')
    generated.mkdir(parents=True, exist_ok=True)
    write_json(generated / 'node.json', {
        'schema_version': 1, 'target': identifier, 'board': resolved['board']['id'],
        'soc': resolved['soc']['id'], 'architecture': resolved['architecture'],
        'workload': resolved['workload']['id'], 'workload_component': resolved['workload']['component'],
    })
    write_json(generated / 'kernel-requirements.json', resolved['kernel_requirements'])
    write_json(generated / 'boot-chain.json', resolved['board']['boot']['chain'])
    write_json(generated / 'access-policy.json', resolved['os']['access_policy'])
    write_json(generated / 'firmware-policy.json', {'schema_version': 1, **resolved['firmware_policy'],
               'files': [{key: item[key] for key in ('id', 'destination', 'sha256', 'license', 'license_file')}
                         for item in resolved['firmware']]})
    write_json(destination / 'installation-policy.json', {
        'schema_version': 1, 'boot_chain': resolved['board']['boot']['chain'],
        'access_policy': resolved['os']['access_policy'],
        'provision_root_filesystems': ['normal', 'rescue'] if resolved['storage']['rescue'] else ['normal'],
        'administrator_helper': '/usr/lib/armhpc/admin.py',
        'administrator_command': ['armhpc', 'firstrun'],
        'normal_entrypoint': '/usr/local/sbin/armhpc',
        'rescue_entrypoint': '/usr/local/sbin/armhpc-rescue',
        'kube_entrypoint': '/usr/local/sbin/armhpc-kube',
        'installer_status': 'local_oect_candidate' if identifier == 'wxy-oect-k3s' else 'not_implemented',
        'note': 'stage 是只读构建输入；OECT 本地候选通过独立组装器接入 Rescue 安装/系统修复，并保留 eMMC 引导区。正常系统和 Rescue 的管理员分别配置。',
    })
    write_json(generated / 'runtime-requirements.json', {
        'cgroup_mode': 'v2' if resolved['setup_policy'] else 'os_default',
        'swap_enabled': False if resolved['setup_policy'] else 'os_default',
        'bootargs_required': ['systemd.unified_cgroup_hierarchy=1'] if resolved['setup_policy'] else [],
        'note': '启动参数必须由板卡启动适配接入；此清单本身不会修改 U-Boot 环境。',
    })
    if resolved['setup_policy']:
        write_json(generated / 'k3s.json', resolved['setup_policy'])
    permissions = {}
    for path in sorted(overlay.rglob('*')):
        if path.is_file():
            relative_path = path.relative_to(overlay).as_posix()
            permissions['/' + relative_path] = '0755' if relative_path.startswith(('usr/local/sbin/', 'etc/initramfs-tools/hooks/', 'etc/initramfs-tools/scripts/')) else '0644'
    permissions['/etc/image-framework/permissions.json'] = '0644'
    write_json(generated / 'permissions.json', permissions)
    for path in generated.iterdir():
        owners[path.relative_to(overlay).as_posix()] = 'framework/generated'
    for item in resolved['inputs']:
        if 'stage_as' in item:
            target = destination / 'board-inputs' / item['stage_as']
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(workspace_path(root, item['path']), target)
    write_json(destination / 'resolved.json', resolved)
    write_json(destination / 'source-receipt.json', {'schema_version': 1, 'inputs': resolved['inputs'],
               'firmware': resolved['firmware'], 'permanent_boot': resolved['permanent_boot']})
    write_json(destination / 'boot-chain.json', {
        'policy': resolved['board']['boot']['chain'], 'components': resolved['permanent_boot'],
        'flash_workflow': resolved['board']['boot'].get('flash_workflow'),
        'assembly_implemented': False, 'temporary_loader_included': False,
    })
    write_json(destination / 'storage-layout.json', storage_layout(resolved))
    write_json(destination / 'overlay-owners.json', owners)
    write_text(destination / 'packages.txt', '\n'.join(resolved['packages']) + '\n')
    backend_inputs(root, destination, resolved, overlay)
    write_json(destination / 'stage-manifest.json', {
        'schema_version': 1, 'framework_version': '0.1.0',
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'target': identifier, 'configuration_valid': True,
        'build_ready': resolved['build_ready'], 'image_built': False,
        'hardware_verified': False, 'blockers': resolved['blockers'],
        'validation_todo': resolved['validation_todo'],
        'files': {p.relative_to(destination).as_posix(): sha256(p) for p in sorted(destination.rglob('*'))
                  if p.is_file() and p.name not in ('.incomplete', 'stage-manifest.json')},
    })
    (destination / '.incomplete').unlink()
    return destination
