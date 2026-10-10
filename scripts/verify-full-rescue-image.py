#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise password SSH, root selection, SATA install and recovery on fixtures."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import time


def run(*args, capture=False, **options):
    result = subprocess.run(list(map(str, args)), check=True, text=True, capture_output=capture, **options)
    return result.stdout.strip() if capture else None


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@contextmanager
def fixture_loop(path):
    device = run('losetup', '--find', '--show', '--partscan', path, capture=True)
    require(Path(run('losetup', '-n', '-O', 'BACK-FILE', device, capture=True)).resolve() == path.resolve(), 'Fixture backing file mismatch')
    try:
        yield device
    finally:
        run('losetup', '--detach', device)


def ssh(port, askpass, command):
    environment = os.environ.copy()
    environment.update(SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE='force', DISPLAY=':armhpc-test')
    return run('ssh', '-p', port, '-o', 'ConnectTimeout=3', '-o', 'StrictHostKeyChecking=no',
               '-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR',
               '-o', 'PreferredAuthentications=password', '-o', 'PubkeyAuthentication=no',
               '-o', 'NumberOfPasswordPrompts=1', 'root@127.0.0.1', command,
               capture=True, env=environment, stdin=subprocess.DEVNULL, timeout=30)


def rescue_login_menu(port, askpass):
    environment = os.environ.copy()
    environment.update(SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE='force', DISPLAY=':armhpc-test')
    result = subprocess.run(['ssh', '-tt', '-p', str(port), '-o', 'ConnectTimeout=3',
                             '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
                             '-o', 'LogLevel=ERROR', '-o', 'PreferredAuthentications=password',
                             '-o', 'PubkeyAuthentication=no', '-o', 'NumberOfPasswordPrompts=1',
                             'root@127.0.0.1'], input='0\nexit\n', text=True, capture_output=True,
                            env=environment, timeout=60, check=True)
    require('ARMHPC Rescue' in result.stdout and '选择 [0]' in result.stdout, 'Interactive SSH did not open the Rescue menu')
    return result.stdout


@contextmanager
def guest(work, emmc, sata, boot, name, port):
    log = work/(name+'-console.log')
    lines = (boot/'extlinux/extlinux.conf').read_text().splitlines()
    append = [line.strip().removeprefix('APPEND ') for line in lines if line.strip().startswith('APPEND ')]
    require(len(append) == 1, 'This board uses one Extlinux boot entry')
    arguments = [part for part in append[0].split() if not part.startswith(('console=', 'earlycon='))]
    arguments.append('console=ttyAMA0,115200')
    command = ['qemu-system-aarch64', '-machine', 'virt', '-cpu', 'cortex-a53', '-m', '1024', '-smp', '2',
               '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-no-reboot',
               '-kernel', str(boot/'Image'),
               '-append', ' '.join(arguments),
               '-drive', 'file='+str(emmc)+',format=raw,if=none,id=emmc', '-device', 'virtio-blk-device,drive=emmc',
               '-netdev', 'user,id=net,hostfwd=tcp:127.0.0.1:'+str(port)+'-:22',
               '-device', 'virtio-net-device,netdev=net,mac=52:54:00:12:34:56']
    if sata:
        command += ['-drive', 'file='+str(sata)+',format=raw,if=none,id=sata', '-device', 'virtio-blk-device,drive=sata']
    # QEMU -initrd strips legacy headers which this board's original loader does
    # not recognize. A generic loader passes the exact published uInitrd bytes.
    dtb = work/(name+'-virt.dtb')
    dump = command.copy()
    dump[dump.index('virt')] = 'virt,dumpdtb='+str(dtb)
    run(*dump, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    initrd = boot/'uInitrd'
    start = 0x60000000
    for property_name, address in (('linux,initrd-start', start), ('linux,initrd-end', start+initrd.stat().st_size)):
        run('fdtput', '-t', 'x', dtb, '/chosen', property_name, '0', f'{address:x}')
    command += ['-dtb', str(dtb), '-device', 'loader,file='+str(initrd)+',addr='+hex(start)+',force-raw=on']
    with log.open('w') as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        try:
            yield process, log
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def wait_ssh(process, port, askpass):
    deadline = time.monotonic()+180
    while time.monotonic() < deadline:
        require(process.poll() is None, 'ARM64 VM stopped before SSH')
        try:
            return ssh(port, askpass, 'id -u')
        except subprocess.SubprocessError:
            time.sleep(2)
    raise RuntimeError('ARM64 password SSH timed out')


def shutdown(process, port, askpass):
    try:
        ssh(port, askpass, 'systemctl poweroff')
    except subprocess.SubprocessError:
        pass
    process.wait(timeout=45)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--build-metadata', type=Path, required=True)
    parser.add_argument('--work-parent', type=Path, required=True)
    parser.add_argument('--password-file', type=Path, required=True)
    args = parser.parse_args()
    project = args.project.resolve(strict=True)
    metadata = json.loads(args.build_metadata.read_text())
    image = Path(metadata['image']['path'])
    build_work = Path(metadata['work_directory'])
    root = build_work/'rescue'
    work = args.work_parent.resolve(strict=True)/('verify-full-rescue-'+str(os.getpid()))
    require(work.is_relative_to(Path('/opt/armbian-hpc/work')) and not work.exists(), 'Use a new isolated fixture directory')
    work.mkdir()
    module = load('armhpc_fixture_rescue', project/'components/rescue/rootfs-overlay/usr/lib/armhpc/rescue.py')
    administrator = load('armhpc_fixture_admin', project/'components/common/rootfs-overlay/usr/lib/armhpc/admin.py')
    module.firstrun_module = lambda: administrator
    module.FACTORY = root/'usr/share/armhpc/factory'
    policy = json.loads((root/'etc/armhpc/rescue.json').read_text())
    checks = {}
    for directory in (root, build_work/'normal'):
        require(not any((directory/'etc/ssh').glob('ssh_host_*')), 'Generic image contains host keys')
        require(not (directory/'etc/image-framework/admin-provisioned.json').exists(), 'Generic image contains an initialized identity')
    sata = work/'sata-70g.img'
    with sata.open('xb') as stream:
        stream.truncate(70*1024**3)
    print('Verifying 64 GiB ext4 install and root-only recovery on a sparse fixture...', flush=True)
    with fixture_loop(sata) as loop:
        disk = {'path':loop, 'size':sata.stat().st_size, 'serial':'fixture-only', 'number':'fixture', 'model':'fixture'}
        # Reproduce the lsblk shape on a disk with an existing partition.
        subprocess.run(['sfdisk', loop], input='label: gpt\nunit: sectors\n\nstart=2048, size=65536, type=L\n',
                       text=True, check=True)
        run('udevadm', 'settle', '--timeout=15')
        flat = json.loads(run('lsblk', '--json', '--bytes', '--output', 'PATH,TYPE,SIZE', loop, capture=True))['blockdevices']
        tree = json.loads(run('lsblk', '--json', '--tree', '--bytes', '--output', 'NAME,PATH,TYPE,SIZE', loop, capture=True))['blockdevices']
        require(len(flat) > 1 and len(tree) == 1 and len(tree[0].get('children', [])) == 1,
                'Could not reproduce and correct the partitioned-disk lsblk shape')
        checks['native_lsblk_partitioned_disk_tree_regression'] = True
        module.do_install(disk, policy, 'root', None)
        parts = module.validate_layout(disk, policy)
        before_table = run('sfdisk', '--dump', loop, capture=True)
        data_uuid = run('blkid', '-s', 'UUID', '-o', 'value', parts[1]['node'], capture=True)
        with module.mounted(parts[1]['node']) as data:
            sentinel = data/'sentinel.txt'
            sentinel.write_text('Preserve this data through root repair\n')
            sentinel.chmod(0o640)
            os.setxattr(sentinel, 'user.armhpc', b'preserve')
            run('setfacl', '-m', 'u:1000:r--', sentinel)
            content, acl = sentinel.read_bytes(), run('getfacl', '-cpn', sentinel, capture=True)
        with module.mounted(parts[0]['node']) as normal:
            (normal/'etc/machine-id').write_text('0a1b2c3d4e5f67890123456789abcdef0\n')
            (normal/'etc/hostname').write_text('armhpc-fixture-node\n')
            (normal/'root/repair-must-remove').write_text('fixture')
            keys = {p.name:digest(p) for p in (normal/'etc/ssh').glob('ssh_host_*')}
        module.do_restore(disk, policy, 'root', None)
        require(before_table == run('sfdisk', '--dump', loop, capture=True), 'Restore changed GPT')
        require(data_uuid == run('blkid', '-s', 'UUID', '-o', 'value', parts[1]['node'], capture=True), 'Restore changed DATA UUID')
        with module.mounted(parts[1]['node'], True) as data:
            sentinel = data/'sentinel.txt'
            require(sentinel.read_bytes() == content and os.getxattr(sentinel, 'user.armhpc') == b'preserve', 'DATA contents changed')
            require(run('getfacl', '-cpn', sentinel, capture=True) == acl, 'DATA ACL changed')
        with module.mounted(parts[0]['node'], True) as normal:
            require(not (normal/'root/repair-must-remove').exists(), 'Root was not reset')
            require((normal/'etc/machine-id').read_text().strip() == '0a1b2c3d4e5f67890123456789abcdef0', 'Machine identity changed')
            require((normal/'etc/hostname').read_text().strip() == 'armhpc-fixture-node', 'Normal hostname changed')
            require(keys == {p.name:digest(p) for p in (normal/'etc/ssh').glob('ssh_host_*')}, 'SSH host identity changed')
        checks['sata_64g_ext4_install_root_restore_data_acl_identity_preserved'] = True
    emmc = work/'emmc-test.img'
    # A standalone fixture avoids a qemu-utils dependency and never writes IMG.
    shutil.copyfile(image, emmc)
    boot = work/'published-boot'
    boot.mkdir()
    # Boot files come from the actual delivered filesystem, not intermediates.
    with fixture_loop(emmc) as loop:
        with module.mounted(loop+'p1', True) as actual:
            for name, expected in (
                ('Image', metadata['boot_files_sha256']['vmlinuz-'+metadata['kernel_release']]),
                ('uInitrd', metadata['initramfs_payload']['sha256']),
                ('config-'+metadata['kernel_release'], metadata['boot_files_sha256']['config-'+metadata['kernel_release']]),
                ('extlinux/extlinux.conf', metadata['boot_files_sha256']['extlinux/extlinux.conf']),
                ('dtb/rockchip/rk3566-wxy-oec-turbo-4g.dtb', metadata['corrected_dtb_sha256'])):
                target = boot/name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(actual/name, target)
                require(digest(target) == expected, 'Published BOOT file differs: '+name)
    sys.path.insert(0, str(project))
    from framework.boot_payload import verify_raw_initramfs
    require(verify_raw_initramfs(boot/'uInitrd', boot/('config-'+metadata['kernel_release']))['sha256'] == metadata['initramfs_payload']['sha256'],
            'Published initramfs is not the declared raw payload')
    checks['published_boot_files_raw_initramfs_verified'] = True
    password = work/'fixture-password'
    shutil.copyfile(args.password_file, password)
    password.chmod(0o600)
    askpass = work/'askpass'
    askpass.write_text('#!/bin/sh\ncat '+str(password)+'\n')
    askpass.chmod(0o700)
    try:
        print('Booting ARM64 without SATA; testing root password SSH and Rescue hostname...', flush=True)
        with guest(work, emmc, None, boot, 'rescue-first', 22239) as (process, log):
            require(wait_ssh(process, 22239, askpass) == '0', 'Password login did not reach root')
            state = json.loads(ssh(22239, askpass, 'cat /run/armhpc/network-identity.json'))
            hostname = ssh(22239, askpass, 'hostname')
            require(hostname == 'armhpc-rescue-'+state['mac'].replace(':', '')[-4:], 'Rescue hostname does not match MAC')
            rgb = json.loads(ssh(22239, askpass, 'armhpc-rescue led --status'))
            require(rgb['mode'] == 'rescue' and rgb['channels']['red'] == 1 and rgb['channels']['green'] == 1
                    and rgb['channels']['heartbeat'], 'Wrong Rescue RGB state')
            ssh(22239, askpass, 'armhpc-rescue led --heartbeat off')
            require(not json.loads(ssh(22239, askpass, 'armhpc-rescue led --status'))['channels']['heartbeat'], 'Heartbeat off failed')
            ssh(22239, askpass, 'armhpc-rescue led --heartbeat on')
            checks['qemu_rescue_yellow_and_heartbeat_toggle'] = True
            status = ssh(22239, askpass, 'armhpc-rescue status')
            require('Factory archives and BOOT files match' in status, 'Recovery factory/BOOT check failed')
            (work/'rescue-status.txt').write_text(status+'\n')
            host_keys = ssh(22239, askpass, 'sha256sum /etc/ssh/ssh_host_*_key')
            (work/'rescue-interactive-login.txt').write_text(rescue_login_menu(22239, askpass))
            checks['qemu_interactive_ssh_opens_rescue_configuration'] = True
            checks['qemu_no_sata_rescue_password_ssh_factory_status_hostname'] = True
            shutdown(process, 22239, askpass)
        print('Rebooting the same ARM64 device; checking persistent MAC and SSH host keys...', flush=True)
        with guest(work, emmc, None, boot, 'rescue-reboot', 22239) as (process, log):
            wait_ssh(process, 22239, askpass)
            reboot_state = json.loads(ssh(22239, askpass, 'cat /run/armhpc/network-identity.json'))
            require(reboot_state['mac'] == state['mac'], 'Random MAC changed after reboot')
            require(ssh(22239, askpass, 'sha256sum /etc/ssh/ssh_host_*_key') == host_keys, 'Rescue SSH host keys changed after reboot')
            checks['qemu_random_mac_persists_across_reboot'] = True
            shutdown(process, 22239, askpass)
        print('Booting installed SATA root; testing password SSH and shared device MAC...', flush=True)
        with guest(work, emmc, sata, boot, 'normal-sata', 22240) as (process, log):
            wait_ssh(process, 22240, askpass)
            details = ssh(22240, askpass, 'uname -r; findmnt -rn --mountpoint / -o SOURCE,FSTYPE; findmnt -rn --mountpoint /data -o SOURCE,FSTYPE; findmnt -rn --mountpoint /boot -o SOURCE,FSTYPE; hostname; armhpc --help')
            (work/'normal-status.txt').write_text(details+'\n')
            expected_hostname='armhpc-'+state['mac'].replace(':','')[-4:]
            require('6.12.111-current-rockchip64' in details and expected_hostname in details, 'Wrong normal root hostname')
            # Virtio disk names depend on probe order. Check labels and parents.
            inspect_mounts = '''import json, subprocess
def output(*args):
    return subprocess.check_output(args, text=True).strip()
mounts = {}
for target in ('/', '/data', '/boot'):
    fs = json.loads(output('findmnt', '--json', '--mountpoint', target, '-o', 'SOURCE,FSTYPE'))['filesystems'][0]
    fs['label'] = output('blkid', '-s', 'LABEL', '-o', 'value', fs['source'])
    fs['parent'] = output('lsblk', '-dn', '-o', 'PKNAME', fs['source'])
    mounts[target] = fs
print(json.dumps(mounts))
'''
            mounts = json.loads(ssh(22240, askpass, 'python3 -c '+shlex.quote(inspect_mounts)))
            for target, label in (('/', policy['root_label']), ('/data', policy['data_label']), ('/boot', policy['boot_label'])):
                require(mounts[target]['fstype'] == 'ext4' and mounts[target]['label'] == label,
                        'Wrong filesystem mounted at '+target)
            require(mounts['/']['parent'] == mounts['/data']['parent'] != mounts['/boot']['parent'],
                    'Root and DATA must share the SATA fixture, separate from eMMC BOOT')
            (work/'normal-mounts.json').write_text(json.dumps(mounts, indent=2)+'\n')
            normal_state = json.loads(ssh(22240, askpass, 'cat /run/armhpc/network-identity.json'))
            require(normal_state['mac'] == state['mac'], 'Normal and Rescue MAC differ')
            rgb = json.loads(ssh(22240, askpass, 'armhpc led --status'))
            require(rgb['mode'] == 'normal' and rgb['channels']['red'] == 0 and rgb['channels']['green'] == 1,
                    'Wrong normal-system RGB state')
            performance = json.loads(ssh(22240, askpass, 'cat /run/armhpc/performance.json'))
            require(not performance['overclock'] and not performance['voltage_table_modified'], 'Performance policy enabled overclock')
            checks['qemu_normal_green_stock_performance_policy'] = True
            require(ssh(22240,askpass,'systemctl is-active chrony.service')=='active','NTP client is not active')
            clock_status=json.loads(ssh(22240,askpass,'cat /run/armhpc/clock-status.json'))
            require(clock_status.get('boot_floor_epoch',clock_status.get('epoch',0))>0,'RTC-less boot floor is absent')
            checks['qemu_normal_mac_hostname_and_clock_service'] = True
            checks['qemu_sata_root_data_password_ssh_shared_mac'] = True
            ssh(22240, askpass, 'armhpc rescue')
            shutdown(process, 22240, askpass)
        print('Testing the one-boot Rescue request with valid SATA attached...', flush=True)
        with guest(work, emmc, sata, boot, 'forced-rescue', 22239) as (process, log):
            wait_ssh(process, 22239, askpass)
            require(ssh(22239, askpass, 'test -f /etc/armhpc/rescue-environment && echo rescue') == 'rescue', 'Forced Rescue was not selected')
            require(ssh(22239, askpass, 'test ! -f /etc/armhpc/force-rescue && echo consumed') == 'consumed', 'One-boot request was not consumed')
            checks['qemu_one_boot_rescue_request'] = True
            shutdown(process, 22239, askpass)
    finally:
        password.unlink(missing_ok=True)
        askpass.unlink(missing_ok=True)
    require(digest(image) == metadata['image']['sha256'], 'Acceptance changed the original IMG')
    report = {'schema_version':1, 'passed':True, 'checks':checks, 'image_sha256':metadata['image']['sha256'],
              'initramfs_loading':'Generic loader preserves the exact uInitrd bytes; no QEMU legacy-header stripping',
              'hardware_verified':False, 'scope':'QEMU ARM64 virtio emulation and isolated loop fixtures; physical RK boot, PHY and SATA controller remain unverified',
              'work_directory':str(work)}
    (args.build_metadata.parent/'rescue-acceptance.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
