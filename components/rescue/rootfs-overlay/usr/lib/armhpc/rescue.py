# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Local Rescue operations for an ext4 boot + external root/data installation."""
import argparse
from contextlib import contextmanager
try:
    import fcntl
except ImportError:
    fcntl = None  # Windows can import the partition planner; execution requires Linux.
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

POLICY = Path('/etc/armhpc/rescue.json')
FACTORY = Path('/usr/share/armhpc/factory')


class RescueError(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise RescueError(message)


def run(*args, capture=False):
    result = subprocess.run(list(map(str, args)), check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C.UTF-8'})
    return result.stdout.strip() if capture else None


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load_policy():
    policy = json.loads(POLICY.read_text())
    require(policy['root_size_bytes'] == 64 * 1024**3, 'This adapter requires a 64 GiB system partition.')
    require(all(re.fullmatch(r'[A-Za-z0-9_]+', policy[key]) for key in ('root_label', 'data_label', 'rescue_label', 'boot_label')), 'Invalid labels.')
    require(re.fullmatch(r'[0-9][A-Za-z0-9.+_-]+', policy['kernel_release']), 'Invalid kernel release.')
    return policy


def assert_rescue(policy):
    require(not Path('/run/armhpc-install-medium').exists(), 'USB commissioning media cannot deploy SATA partitions.')
    require(os.geteuid() == 0 and sys.platform == 'linux', 'Run as root in the independent Rescue.')
    require(Path('/etc/armhpc/rescue-environment').is_file(), 'This command requires the Rescue environment.')
    filesystem = json.loads(run('findmnt', '--json', '--mountpoint', '/', '-o', 'SOURCE,FSTYPE', capture=True))['filesystems'][0]
    if filesystem['fstype'] == 'overlay':
        sys.path.insert(0, '/usr/lib/armhpc-ota')
        from armhpc_ota.config import Config
        from armhpc_ota.device import discover, layout
        config = Config.load(); device = discover(config); storage = layout(config, device, initialized=True)
        require(storage['board'].get('rescue_fstype') == 'squashfs' and device['root'] in
                [storage['partitions'][r]['path'] for r in ('rescue_a', 'rescue_b')], 'Wrong SquashFS Rescue root.')
        require(os.uname().release == policy['kernel_release'], 'Factory kernel differs from the running kernel.')
        return
    require(filesystem['fstype'] == 'ext4', 'Rescue root must be ext4.')
    require(run('blkid', '-s', 'LABEL', '-o', 'value', filesystem['source'], capture=True) == policy['rescue_label'], 'Running root is not the configured Rescue partition.')
    require(os.uname().release == policy['kernel_release'], 'Running kernel differs from the factory kernel.')


def check_factory(policy):
    manifest = json.loads((FACTORY / 'manifest.json').read_text())
    require(manifest['kernel_release'] == policy['kernel_release'] and manifest['target'] == policy['target'], 'Factory target or kernel mismatch.')
    for name in ('factory-rootfs.tar.zst', 'boot-files.tar.zst'):
        require(file_hash(FACTORY / name) == manifest['files'][name]['sha256'], 'Factory archive checksum mismatch: ' + name)
    boot = Path('/boot')
    mounted = json.loads(run('findmnt', '--json', '--mountpoint', boot, '-o', 'SOURCE,FSTYPE,OPTIONS', capture=True))['filesystems'][0]
    require(mounted['fstype'] == policy.get('boot_fstype','ext4') and 'ro' in mounted['options'].split(','), 'The eMMC BOOT filesystem must be mounted read only.')
    require(run('blkid', '-s', 'LABEL', '-o', 'value', mounted['source'], capture=True) == policy['boot_label'], 'Wrong BOOT filesystem.')
    for relative, digest in manifest['boot_files'].items():
        require(file_hash(boot / relative) == digest, 'BOOT files do not match the factory: ' + relative)
    return manifest


def disk_info(device):
    supplied = Path(device)
    require(supplied.is_absolute() and supplied.parts[:2] == ('/', 'dev'),
            '请输入完整 /dev/ 磁盘路径，或在菜单中使用磁盘编号。')
    try:
        path = supplied.resolve(strict=True)
    except FileNotFoundError:
        raise RescueError('目标设备不存在：'+repr(str(supplied))+'；请重新选择已列出的 SATA 磁盘。') from None
    require(stat.S_ISBLK(path.stat().st_mode), 'Select a block device, not a file.')
    # Without NAME or --tree, lsblk JSON places partitions beside their disk.
    # Request the hierarchy explicitly so children are checked before writing.
    items = json.loads(run('lsblk', '--json', '--tree', '--bytes', '--output', 'NAME,PATH,TYPE,SIZE,MODEL,SERIAL,WWN,TRAN,MAJ:MIN,MOUNTPOINTS', path, capture=True))['blockdevices']
    require(len(items) == 1, 'Ambiguous target disk.')
    info = items[0]
    require(info.get('path') == str(path), 'Target disk identity does not match its resolved path.')
    require(info['type'] == 'disk' and info['tran'] == 'sata', 'Select a whole SATA disk. eMMC, USB, partitions and loop devices are refused.')
    require(run('blockdev', '--getss', path, capture=True) == '512', 'This installer requires 512-byte logical sectors.')
    serial = (info.get('serial') or '').strip()
    wwn = (info.get('wwn') or '').strip()
    require((serial or wwn) and all(not value or re.fullmatch(r'[A-Za-z0-9_.:+ -]+', value) for value in (serial,wwn)), 'Disk must have a readable serial number or WWN.')
    def walk(node):
        require(not any(node.get('mountpoints') or []), 'Target disk or a child is mounted.')
        for child in node.get('children', []):
            walk(child)
    walk(info)
    target_number = info['maj:min']
    for mount in ('/', '/boot'):
        source = run('findmnt', '-rn', '--mountpoint', mount, '-o', 'SOURCE', capture=True)
        if source == 'overlay':
            sys.path.insert(0, '/usr/lib/armhpc-ota')
            from armhpc_ota.config import Config
            from armhpc_ota.device import discover
            source = discover(Config.load())['root']
        ancestors = run('lsblk', '--inverse', '--noheadings', '--output', 'MAJ:MIN', source, capture=True).split()
        require(target_number not in ancestors, 'Refusing the disk containing Rescue or BOOT.')
    swap = run('swapon', '--show', '--noheadings', '--raw', '--output', 'NAME', capture=True).splitlines()
    for source in swap:
        if Path(source).is_block_device():
            require(target_number not in run('lsblk', '--inverse', '--noheadings', '--output', 'MAJ:MIN', source, capture=True).split(), 'Target disk has active swap.')
    # Check holders on the disk and each partition (dm-crypt, MD, LVM).
    def descendants(node):
        yield node
        for child in node.get('children', []):
            yield from descendants(child)
    nodes = list(descendants(info))
    require(all(not any((Path('/sys/dev/block') / node['maj:min'] / 'holders').iterdir()) for node in nodes), 'Target is in use by a mapped block device.')
    return {'path': str(path), 'size': int(info['size']), 'serial': serial, 'wwn':wwn, 'number': target_number, 'model': info['model']}


def identity(disk):
    return tuple(disk.get(key) for key in ('path', 'size', 'serial', 'wwn', 'number'))


def partitions(device):
    table = json.loads(run('sfdisk', '--json', device, capture=True))['partitiontable']
    require(table['label'] == 'gpt' and table.get('sectorsize', 512) == 512, 'Disk must use GPT with 512-byte logical sectors.')
    entries = table['partitions']
    require(len(entries) == 2, 'Expected exactly root and data partitions.')
    return entries


def partition_plan(size, root_size):
    alignment = 1024**2
    data_end = ((size - 33 * 512) // alignment) * alignment
    data_start = alignment + root_size
    require(data_end - data_start >= 16 * alignment, 'Disk needs 64 GiB root plus GPT and at least 16 MiB data.')
    return [{'start': alignment // 512, 'size': root_size // 512},
            {'start': data_start // 512, 'size': (data_end - data_start) // 512}]


def validate_layout(disk, policy):
    actual = partitions(disk['path'])
    expected = partition_plan(disk['size'], policy['root_size_bytes'])
    require(all(int(item['start']) == plan['start'] and int(item['size']) == plan['size'] for item, plan in zip(actual, expected)), 'Partition boundaries differ from the configured 64 GiB layout.')
    for item, label in zip(actual, (policy['root_label'], policy['data_label'])):
        require(run('blkid', '-s', 'TYPE', '-o', 'value', item['node'], capture=True) == 'ext4', 'Both partitions must be ext4.')
        require(run('blkid', '-s', 'LABEL', '-o', 'value', item['node'], capture=True) == label, 'Unexpected partition label.')
    return actual


@contextmanager
def mounted(device, read_only=False):
    with tempfile.TemporaryDirectory(prefix='armhpc-rescue-', dir='/run') as directory:
        run('mount', '-t', 'ext4', '-o', 'ro,noload' if read_only else 'rw', device, directory)
        try:
            yield Path(directory)
        finally:
            run('umount', directory)


def firstrun_module():
    loader = importlib.machinery.SourceFileLoader('armhpc_firstrun', '/usr/lib/armhpc/admin.py')
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def check_administrator(username, key):
    if key is None:
        require(username == 'root', 'Without a public key, use the factory root password profile.')
        manifest = json.loads((FACTORY / 'manifest.json').read_text())
        require(manifest.get('authentication') == 'root_password', 'Factory password profile is unavailable; supply an administrator and public key.')
        return
    tool = firstrun_module()
    tool.validate_username(username)
    tool.parse_public_key(Path(key).read_text())


def restore_root(device, policy, username, key, retained=None):
    run('mkfs.ext4', '-F', '-m', '1', '-L', policy['root_label'], device)
    with mounted(device) as root:
        run('tar', '--warning=no-timestamp', '--zstd', '--numeric-owner', '--acls', '--xattrs', '--xattrs-include=*', '-xpf', FACTORY / 'factory-rootfs.tar.zst', '-C', root)
        require((root / 'lib/modules' / policy['kernel_release']).is_dir(), 'Restored modules do not match BOOT.')
        administrator = firstrun_module()
        if key is not None:
            administrator.provision(root, username, key)
        else:
            profile = json.loads((root / 'etc/armhpc/authentication.json').read_text())
            require(profile.get('authentication') == 'root_password', 'Factory root password profile is missing.')
            (root / 'run/sshd').mkdir(parents=True, exist_ok=True)
            administrator.in_target(root, '/usr/bin/ssh-keygen', '-A')
            administrator.in_target(root, '/usr/sbin/sshd', '-t')
            marker = root / 'etc/image-framework/admin-provisioned.json'
            marker.write_text(json.dumps({'schema_version': 1, 'username': 'root', 'authentication': 'root_password'}) + '\n')
        for relative, content in (retained or {}).items():
            path = root / relative
            path.write_bytes(content)
            path.chmod(0o600 if path.name.endswith('_key') else 0o644)
        administrator.in_target(root, '/usr/sbin/sshd', '-t')
        complete = root / 'etc/armhpc/installation-complete.json'
        complete.parent.mkdir(parents=True, exist_ok=True)
        complete.write_text(json.dumps({'kernel_release': policy['kernel_release'], 'target': policy['target']}) + '\n')
        register_factory_system(root, device)
        run('sync', '-f', root)


def register_factory_system(root, device):
    if not (Path('/run/armhpc/ota-boot.json')).is_file(): return
    sys.path.insert(0, '/usr/lib/armhpc-ota')
    from armhpc_ota.config import Config
    from armhpc_ota.device import discover, layout
    from armhpc_ota.engine import Engine
    from armhpc_ota.io import atomic_json
    config = Config.load()
    if config.board().get('rescue_fstype') != 'squashfs': return
    engine = Engine(config); storage = layout(config, discover(config), initialized=True)
    require(storage['partitions']['system']['path'] == str(Path(device).resolve()), 'New SATA root differs from its adapter.')
    from armhpc_ota.maintenance import retain_installed_runtime
    retain_installed_runtime(config, root)
    engine.backend.configure_shared_mount(root, storage, 'system')
    engine.backend.restore_shared_identity(root, 'system')
    engine.backend.save_shared_identity(root, 'system')
    control = engine.store.control()
    initial = json.loads((config.state / 'baselines/initial.json').read_text())['release']
    manifest = engine.manifest(release=initial)
    require(json.loads((root / 'etc/armhpc/release.json').read_text())['release'] == initial, 'Factory release is not the initial OTA baseline.')
    reference = engine.backend.boot_files({'release': initial, 'manifest': manifest}, 'system')
    control['confirmed']['system'] = {'boot': reference}
    control['system_blocked'] = False
    control['next'] = {'target': 'system', 'slot': control['confirmed']['rescue']['slot'], 'trial': False}
    engine.store.write_control(control)


def retained_identity(device):
    result = {}
    with mounted(device, read_only=True) as root:
        transaction = root / 'var/lib/armhpc/network-config/transaction.json'
        if transaction.exists():
            require(not transaction.is_symlink() and json.loads(transaction.read_text()).get('stage') in ('confirmed', 'rolled-back', 'failed'),
                    'Confirm or roll back the pending network configuration before factory restoration.')
        candidates = [root / 'etc/machine-id', root / 'etc/hostname', root / 'etc/hosts', *(root / 'etc/ssh').glob('ssh_host_*_key*')]
        for path in candidates:
            require(not path.is_symlink(), 'Identity file cannot be a symbolic link.')
            if path.is_file():
                result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def do_install(disk, policy, username, key):
    plan = partition_plan(disk['size'], policy['root_size_bytes'])
    text = 'label: gpt\nunit: sectors\n\n' + ''.join(f"start={p['start']}, size={p['size']}, type=L\n" for p in plan)
    subprocess.run(['sfdisk', '--wipe', 'always', '--wipe-partitions', 'always', disk['path']], input=text, text=True, check=True)
    run('udevadm', 'settle', '--timeout=15')
    entries = partitions(disk['path'])
    require(all(int(item['start']) == p['start'] and int(item['size']) == p['size'] for item, p in zip(entries, plan)), 'New partition geometry did not match the plan.')
    run('mkfs.ext4', '-F', '-m', '0', '-L', policy['data_label'], entries[1]['node'])
    restore_root(entries[0]['node'], policy, username, key)
    validate_layout(disk, policy)


def do_restore(disk, policy, username, key):
    entries = validate_layout(disk, policy)
    table_before = run('sfdisk', '--dump', disk['path'], capture=True)
    data_identity = run('blkid', '-s', 'UUID', '-o', 'value', entries[1]['node'], capture=True)
    retained = retained_identity(entries[0]['node'])
    # Format root only. DATA is never mounted or written by this operation.
    restore_root(entries[0]['node'], policy, username, key, retained)
    require(run('sfdisk', '--dump', disk['path'], capture=True) == table_before, 'Partition table changed during root repair.')
    require(run('blkid', '-s', 'UUID', '-o', 'value', entries[1]['node'], capture=True) == data_identity, 'DATA identity changed during repair.')

def indicate_write_failure(reason):
    path = Path('/usr/lib/armhpc/leds.py')
    if path.is_file():
        try:
            loader = importlib.machinery.SourceFileLoader('armhpc_rescue_leds', str(path))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            module.mark_error(reason)
        except (OSError, ValueError) as error:
            print('写盘失败的 LED 提示未设置：'+str(error), file=sys.stderr)


def confirm_write(action, disk):
    print(f"目标磁盘：{disk['path']}  型号：{disk['model']}  容量：{disk['size']/1024**3:.2f} GiB")
    print('序列号：'+disk['serial'])
    print('将清空此 SATA 整盘并创建系统／数据分区。' if action == 'install'
          else '将重建系统分区，保留数据分区。')
    while True:
        answer = input('确认继续写盘？[y/N]: ').strip().lower()
        if answer in ('y', 'yes'):
            return True
        if answer in ('', 'n', 'no'):
            return False
        print('请输入 y 或 n。')


def main(action, argv=None):
    parser = argparse.ArgumentParser(prog="armhpc-rescue " + action, description={'install': '在 SATA 创建 64 GiB ext4 系统和剩余 ext4 数据分区。', 'restore': '仅恢复 SATA 系统分区，保留数据和机器身份。', 'status': '查看救援环境、磁盘及工厂包状态。', 'provision': '从本地终端配置 Rescue 管理员和 SSH 公钥。'}[action])
    if action in ('install', 'restore'):
        parser.add_argument('--disk', type=Path, required=True)
        parser.add_argument('--username', default='root', help='默认使用工厂系统的 root 登录配置。')
        parser.add_argument('--public-key-file', type=Path, help='可选：为指定的普通管理员导入 SSH 公钥。')
        parser.add_argument('--execute', action='store_true', help='执行写盘；默认只显示计划。还需交互输入确认。')
    elif action == 'provision':
        parser.add_argument('--username', required=True)
        parser.add_argument('--public-key-file', type=Path, required=True)
    args = parser.parse_args(argv)
    policy = load_policy()
    assert_rescue(policy)
    if action == 'status':
        print('Rescue hostname: ' + Path('/etc/hostname').read_text().strip())
        run('ip', '-br', 'address')
        run('ss', '-lnt')
        state = Path('/run/armhpc/network-identity.json')
        if state.is_file():
            print(state.read_text())
        run('lsblk', '--bytes', '-o', 'NAME,TYPE,SIZE,MODEL,SERIAL,TRAN,FSTYPE,LABEL,MOUNTPOINTS')
        check_factory(policy)
        print('Factory archives and BOOT files match: ' + policy['kernel_release'])
        return
    if action == 'provision':
        require(sys.stdin.isatty() and not os.environ.get('SSH_CONNECTION'), 'Provision Rescue from a local interactive console.')
        firstrun_module().provision(Path('/'), args.username, args.public_key_file, rescue=True)
        run('systemctl', 'restart', 'ssh.service')
        return
    check_administrator(args.username, args.public_key_file)
    check_factory(policy)
    disk = disk_info(args.disk)
    children = {entry['node'] for entry in partitions(disk['path'])} if action == 'restore' else set()
    if action == 'install':
        children = set(run('lsblk', '-rn', '-o', 'PATH', disk['path'], capture=True).splitlines())
    for label in (policy['root_label'], policy['data_label']):
        found = subprocess.run(['blkid', '-c', '/dev/null', '-t', 'LABEL=' + label, '-o', 'device'], text=True, capture_output=True)
        require(found.returncode in (0, 2), 'Could not enumerate filesystem labels.')
        require(all(str(Path(path).resolve()) in children for path in found.stdout.splitlines()), 'Configured label also exists on another disk: ' + label)
    plan = partition_plan(disk['size'], policy['root_size_bytes'])
    if action == 'restore':
        validate_layout(disk, policy)
    print(json.dumps({'action': action, 'disk': disk, 'partitions': plan, 'data_preserved': action == 'restore', 'execute': args.execute}, ensure_ascii=False, indent=2))
    if not args.execute:
        return
    require(sys.stdin.isatty() and sys.stdout.isatty(), 'Writing requires an interactive terminal.')
    if not confirm_write(action, disk):
        print('已取消，磁盘未写入。')
        return
    require(identity(disk_info(args.disk)) == identity(disk), 'Target disk changed since confirmation.')
    sys.path.insert(0, '/usr/lib/armhpc-ota')
    from armhpc_ota.io import writer_lock
    from armhpc_ota.config import Config
    from armhpc_ota.store import Store, TERMINAL
    with writer_lock(Config.load().global_writer_lock), open('/run/armhpc-rescue-disk.lock', 'w') as lock:
        if (Config.load().state / 'control.json').is_file():
            job = Store(Config.load().state).read()
            require(not job or job['stage'] in TERMINAL, 'An OTA job is active; finish it before factory disk operations.')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(identity(disk_info(args.disk)) == identity(disk), 'Target disk changed since confirmation.')
        try:
            (do_install if action == 'install' else do_restore)(disk, policy, args.username, args.public_key_file)
        except (OSError, ValueError, RescueError, subprocess.SubprocessError, KeyboardInterrupt):
            indicate_write_failure('SATA '+action+' failed after write confirmation; the system partition may be incomplete')
            raise
    print('完成。eMMC 引导区未改写；重启后进入 SATA 系统，再通过 SSH 运行 armhpc configuration。')


def entry(action, argv=None):
    try:
        main(action, argv)
    except (RescueError, OSError, ValueError, subprocess.SubprocessError) as error:
        print('Rescue 操作未完成：' + str(error), file=sys.stderr)
        return 1
    return 0
