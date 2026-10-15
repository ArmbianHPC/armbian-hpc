# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Configure device network identity and the selected SSH authentication profile."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import subprocess
import tempfile

VENDOR_TAG = 0x524B5644
VENDOR_START = 7168 * 512
VENDOR_SLOT_SIZE = 128 * 512
VENDOR_READ_IOCTL = 0x40047601
STATE = 'var/lib/armhpc/network-identity.json'


def run(*args, capture=False):
    result = subprocess.run(list(map(str, args)), check=True, text=True,
                            capture_output=capture, timeout=30)
    return result.stdout.strip() if capture else None


def atomic_write(path, content, mode=0o644):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing a symbolic-link configuration file: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.stat() if path.exists() else None
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        if old:
            os.fchown(descriptor, old.st_uid, old.st_gid)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def valid_mac(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', value):
        return False
    raw = bytes.fromhex(value.replace(':', ''))
    return raw != bytes(6) and not raw[0] & 1


def format_mac(raw):
    return ':'.join(f'{byte:02x}' for byte in raw)


def random_mac():
    raw = bytearray(secrets.token_bytes(6))
    raw[0] = (raw[0] & 0xfe) | 0x02
    return format_mac(raw)


def rescue_hostname(mac):
    if not valid_mac(mac):
        raise ValueError('Invalid MAC for Rescue hostname')
    return 'armhpc-rescue-' + mac.replace(':', '')[-4:].lower()


def normal_hostname(mac):
    if not valid_mac(mac):
        raise ValueError('Invalid MAC for normal hostname')
    return 'armhpc-' + mac.replace(':', '')[-4:].lower()


def configure_hostname(mac, root=Path('/'), runtime=True):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from hostname_config import boot_hostname, update_hosts
    from config_io import safe_path
    root = Path(root)
    rescue = (root / 'etc/armhpc/rescue-environment').is_file()
    path = safe_path(root, 'etc/hostname')
    current = path.read_text().strip() if path.is_file() else ''
    generated = rescue_hostname(mac) if rescue else normal_hostname(mac)
    hostname = boot_hostname(current, generated, rescue)
    hosts_path = safe_path(root, 'etc/hosts')
    hosts = hosts_path.read_text() if hosts_path.is_file() else '127.0.0.1 localhost\n'
    atomic_write(hosts_path, update_hosts(hosts, hostname))
    atomic_write(path, hostname + '\n')
    if runtime:
        socket.sethostname(hostname.encode('ascii'))
    return hostname


def recover_network_settings():
    if Path('/etc/armhpc/rescue-environment').is_file() or not Path('/var/lib/armhpc/network-config/transaction.json').is_file():
        return
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from network_config import Network
    Network().recover_on_boot()


def vendor_record(raw, mac_index=0):
    """Read only LAN_MAC_ID from a bounded Rockchip eMMC vendor-storage slot.

    Layout: Rockchip sdmmc_vendor_storage.c, 64 KiB slots, 126 item entries.
    The upstream format uses a tag and mirrored version, not a verified CRC.
    """
    if len(raw) != VENDOR_SLOT_SIZE or mac_index not in (0, 1):
        return None
    tag, version, next_index, count, free_offset, free_size = struct.unpack_from('<IIHHHH', raw)
    if tag != VENDOR_TAG or not version or version != struct.unpack_from('<I', raw, len(raw)-4)[0]:
        return None
    if next_index >= 4 or count > 126 or free_offset + free_size > len(raw)-1032:
        return None
    ids, lan = set(), None
    for number in range(count):
        item, offset, size, flags = struct.unpack_from('<HHHH', raw, 16 + number * 8)
        if item in ids or offset + size > len(raw)-1032:
            return None
        ids.add(item)
        if item == 3:
            start = 1024 + offset + 6 * mac_index
            if size >= 6 * (mac_index + 1):
                candidate = format_mac(raw[start:start+6])
                if valid_mac(candidate):
                    lan = candidate
    return {'version': version, 'mac': lan}


def stored_vendor_mac(device, mac_index=0):
    # Support kernels exposing the official read ioctl; never issue a write ioctl.
    vendor = Path('/dev/vendor_storage')
    if vendor.exists() and stat.S_ISCHR(vendor.stat().st_mode):
        request = bytearray(1032)
        struct.pack_into('<IHH', request, 0, 0x56524551, 3, 12)
        try:
            with vendor.open('rb', buffering=0) as stream:
                fcntl.ioctl(stream.fileno(), VENDOR_READ_IOCTL, request, True)
            tag, item, length = struct.unpack_from('<IHH', request)
            if tag == 0x56524551 and item == 3 and 6*(mac_index+1) <= length <= 12:
                mac = format_mac(request[8+mac_index*6:14+mac_index*6])
                if valid_mac(mac):
                    return mac, 'rockchip-vendor-ioctl'
        except OSError:
            pass
    if device is None:
        return None, None
    records = []
    try:
        with Path(device).open('rb', buffering=0) as stream:
            for index in range(4):
                stream.seek(VENDOR_START + index*VENDOR_SLOT_SIZE)
                record = vendor_record(stream.read(VENDOR_SLOT_SIZE), mac_index)
                if record:
                    records.append(record)
    except OSError:
        return None, None
    if records:
        newest = max(records, key=lambda item: item['version'])
        if newest['mac']:
            return newest['mac'], 'rockchip-emmc-vendor-storage'
    return None, None


def choose_identity(vendor_mac, saved, cid=None):
    if valid_mac(vendor_mac):
        return vendor_mac.lower(), 'vendor-storage'
    if isinstance(saved, dict) and valid_mac(saved.get('mac')) and (not cid or saved.get('emmc_cid') == cid):
        return saved['mac'].lower(), 'persisted-' + saved.get('origin', 'random')
    return random_mac(), 'random'


def labelled_partition(label, filesystem='ext4'):
    result = subprocess.run(['blkid', '-c', '/dev/null', '-t', 'LABEL=' + label, '-o', 'device'],
                            text=True, capture_output=True, timeout=10)
    found = result.stdout.splitlines() if result.returncode in (0, 2) else []
    if len(found) != 1:
        raise ValueError('Expected one filesystem with label ' + label)
    device = Path(found[0]).resolve(strict=True)
    if not device.is_block_device() or run('blkid', '-s', 'TYPE', '-o', 'value', device, capture=True) != filesystem:
        raise ValueError('Identity store must be a block-device '+filesystem+' partition')
    return device


def parent_disk(device):
    parent = run('lsblk', '-dn', '-o', 'PKNAME', device, capture=True)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', parent):
        raise ValueError('Identity store must be a disk partition')
    return Path('/dev') / parent


@contextmanager
def identity_store():
    policy = json.loads(Path('/etc/armhpc/network-identity.json').read_text())
    shared_root = Path('/var/lib/armhpc/ota')
    if (shared_root/'identity-root').is_dir():
        shared = labelled_partition('OTA_SHARED')
        boot = labelled_partition(policy['boot_label'],policy.get('boot_fstype','ext4'))
        active_shared = Path(run('findmnt', '-rn', '--mountpoint', str(shared_root), '-o', 'SOURCE', capture=True).split('[')[0]).resolve()
        if active_shared != shared or parent_disk(shared) != parent_disk(boot):
            raise ValueError('OTA shared identity is not on the expected eMMC mount')
        yield shared_root/'identity-root', parent_disk(shared), policy
        return
    boot_record = Path('/run/armhpc/ota-boot.json')
    if boot_record.is_file():
        record = json.loads(boot_record.read_text())
        if record.get('target') == 'rescue' and record.get('fallback') and not record.get('shared_ready'):
            disk = parent_disk(Path(record['root']))
            yield Path('/'), disk, policy
            return
    rescue = labelled_partition(policy['rescue_label'])
    boot = labelled_partition(policy['boot_label'])
    disk = parent_disk(rescue)
    if disk != parent_disk(boot):
        raise ValueError('BOOT and Rescue are on different disks')
    if Path('/etc/armhpc/rescue-environment').is_file():
        active = Path(run('findmnt', '-rn', '--mountpoint', '/', '-o', 'SOURCE', capture=True)).resolve()
        if active != rescue:
            raise ValueError('Rescue identity store is not the running root')
        yield Path('/'), disk, policy
    else:
        with tempfile.TemporaryDirectory(prefix='armhpc-identity-', dir='/run') as directory:
            run('mount', '-t', 'ext4', '-o', 'rw,nosuid,nodev,noexec', rescue, directory)
            try:
                if not (Path(directory)/'etc/armhpc/rescue-environment').is_file():
                    raise ValueError('Shared identity store is not Rescue')
                yield Path(directory), disk, policy
                run('sync', '-f', directory)
            finally:
                run('umount', directory)


def interface_name():
    candidates = [p.name for p in Path('/sys/class/net').iterdir()
                  if re.fullmatch(r'(?:eth|en)[A-Za-z0-9_.-]*', p.name) and (p/'device').exists()]
    if not candidates:
        raise ValueError('No physical Ethernet interface')
    return 'eth0' if 'eth0' in candidates else sorted(candidates)[0]


def configure_network():
    ports_policy=Path('/etc/armhpc/network-ports.json')
    if ports_policy.is_file() and json.loads(ports_policy.read_text()).get('provider')=='amlogic':
        import sys
        sys.path.insert(0,str(Path(__file__).resolve().parent))
        from network_ports import configure
        with identity_store() as (store,disk,policy): selected=configure(store)
        configure_hostname(selected)
        return
    name = interface_name()
    with identity_store() as (store, disk, policy):
        info = Path('/sys/class/block')/disk.name/'device'
        cid_path = info/'cid'
        cid = cid_path.read_text().strip() if cid_path.is_file() else None
        raw_device = disk if (info/'type').is_file() and (info/'type').read_text().strip() == 'MMC' else None
        mac, detail = stored_vendor_mac(raw_device, policy.get('vendor_mac_index', 0))
        try:
            saved = json.loads((store/STATE).read_text())
        except (OSError, ValueError):
            saved = None
        selected, source = choose_identity(mac, saved, cid)
        run('ip', 'link', 'set', 'dev', name, 'down')
        run('ip', 'link', 'set', 'dev', name, 'address', selected)
        origin = 'vendor-storage' if mac else saved.get('origin', 'random') if source.startswith('persisted-') else 'random'
        receipt = {'schema_version': 1, 'interface': name, 'mac': selected, 'origin': origin,
                   'source': detail or source, 'emmc_cid': cid}
        atomic_write(store/STATE, json.dumps(receipt, indent=2)+'\n', 0o600)
        atomic_write(Path('/run/armhpc/network-identity.json'), json.dumps(receipt, indent=2)+'\n')
    configure_hostname(selected)
    if Path('/etc/armhpc/rescue-environment').is_file():
        Path('/etc/armhpc/force-rescue').unlink(missing_ok=True)
    print('ARMHPC Ethernet: ' + name + ' ' + selected + ' (' + receipt['source'] + ')', flush=True)


def prepare_password_profile(root, password_hash=None):
    root = Path(root)
    if password_hash is not None:
        if not re.fullmatch(r'\$6\$[./A-Za-z0-9]+\$[./A-Za-z0-9]+', password_hash):
            raise ValueError('An encrypted SHA-512 password is required')
        shadow = root/'etc/shadow'
        lines = shadow.read_text().splitlines()
        found = 0
        for index, line in enumerate(lines):
            fields = line.split(':')
            if fields[0] == 'root':
                found += 1
                fields[1] = password_hash
                fields[2] = str(int(datetime.now(timezone.utc).timestamp())//86400)
                fields[7] = ''
                lines[index] = ':'.join(fields)
        if found != 1:
            raise ValueError('Expected one root shadow entry')
        atomic_write(shadow, '\n'.join(lines)+'\n', shadow.stat().st_mode & 0o777)
    config = root/'etc/ssh/sshd_config'
    include = 'Include /etc/ssh/image-framework-admin.conf'
    atomic_write(config, include+'\n'+'\n'.join(line for line in config.read_text().splitlines() if line.strip() != include)+'\n')
    atomic_write(root/'etc/ssh/image-framework-admin.conf',
                 'PermitRootLogin yes\nPasswordAuthentication yes\nKbdInteractiveAuthentication no\n'
                 'PubkeyAuthentication yes\nAuthenticationMethods any\nPermitEmptyPasswords no\n'
                 'UsePAM yes\nAllowUsers root\n')
    atomic_write(root/'etc/armhpc/authentication.json', json.dumps({'authentication': 'root_password', 'username': 'root'})+'\n')
    (root/'root/.not_logged_in_yet').unlink(missing_ok=True)
    for unit in ('ssh.service', 'ssh.socket'):
        atomic_write(root/f'etc/systemd/system/{unit}.d/10-image-framework-admin.conf',
                     '[Unit]\nConditionPathExists=/etc/image-framework/admin-provisioned.json\n'
                     'Wants=armhpc-firstboot.service\nAfter=armhpc-firstboot.service\n')


def complete_password_access(root=Path('/')):
    root = Path(root)
    profile = json.loads((root/'etc/armhpc/authentication.json').read_text())
    if profile.get('authentication') != 'root_password':
        return
    root_records = [line.split(':') for line in (root/'etc/shadow').read_text().splitlines() if line.startswith('root:')]
    if len(root_records) != 1 or not root_records[0][1].startswith('$'):
        raise ValueError('Root password was not initialized')
    if root == Path('/'):
        shared = Path('/var/lib/armhpc/ota')
        ota = None
        if shared.is_mount() and Path('/run/armhpc/ota-boot.json').is_file() and not Path('/run/armhpc-install-medium').exists():
            import sys
            sys.path.insert(0, '/usr/lib/armhpc-ota')
            from armhpc_ota.config import Config
            from armhpc_ota.backend import Backend
            ota = Backend(Config.load())
            ota.restore_shared_identity(root, 'rescue' if (root/'etc/armhpc/rescue-environment').is_file() else 'system')
        run('ssh-keygen', '-A')
        if ota: ota.save_shared_identity(root, 'rescue' if (root/'etc/armhpc/rescue-environment').is_file() else 'system')
        Path('/run/sshd').mkdir(exist_ok=True)
        run('/usr/sbin/sshd', '-t')
    else:
        # This branch is used only by the validated offline SATA installer.
        import importlib.machinery
        tool = importlib.machinery.SourceFileLoader('armhpc_password_admin', '/usr/lib/armhpc/admin.py').load_module()
        (root/'run/sshd').mkdir(parents=True, exist_ok=True)
        tool.in_target(root, '/usr/bin/ssh-keygen', '-A')
        tool.in_target(root, '/usr/sbin/sshd', '-t')
    atomic_write(root/'etc/image-framework/admin-provisioned.json',
                 json.dumps({'schema_version': 1, 'username': 'root', 'authentication': 'root_password',
                             'created_utc': datetime.now(timezone.utc).isoformat()})+'\n')
    print('ARMHPC SSH password profile initialized.', flush=True)


def request_rescue(cancel=False):
    if Path('/var/lib/armhpc/ota/control.json').is_file():
        import sys
        sys.path.insert(0, '/usr/lib/armhpc-ota')
        from armhpc_ota.config import Config
        from armhpc_ota.engine import Engine
        Engine(Config.load()).request_rescue_boot(cancel)
        return
    with identity_store() as (store, disk, policy):
        flag = store/'etc/armhpc/force-rescue'
        if cancel:
            flag.unlink(missing_ok=True)
        else:
            atomic_write(flag, 'One boot requested by a local administrator\n', 0o600)


def main():
    if os.geteuid() != 0:
        raise RuntimeError('First boot configuration requires root')
    try:
        recover_network_settings()
    except (OSError, ValueError, RuntimeError) as error:
        print('ARMHPC network configuration recovery failed: ' + str(error), flush=True)
    try:
        configure_network()
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as error:
        # Keep the kernel's usable address when storage/interface discovery fails.
        # Authentication still initializes so an administrator can diagnose it.
        print('ARMHPC network identity initialization failed: ' + str(error), flush=True)
    profile = Path('/etc/armhpc/authentication.json')
    if profile.is_file():
        complete_password_access()
    else:
        run('ssh-keygen', '-A')
        Path('/run/sshd').mkdir(exist_ok=True)


if __name__ == '__main__':
    main()
