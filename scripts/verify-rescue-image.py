#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify disk archives, root repair and ARM64 boots on disposable local images."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def run(*args, capture=False):
    result = subprocess.run(list(map(str, args)), check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout.strip() if capture else None


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(value, message):
    if not value:
        raise RuntimeError(message)


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@contextmanager
def loop_file(path, read_only=False):
    options = ['--read-only'] if read_only else []
    device = run('losetup', '--find', '--show', '--partscan', *options, path, capture=True)
    require(device.startswith('/dev/loop') and Path(run('losetup', '-n', '-O', 'BACK-FILE', device, capture=True)).resolve() == path.resolve(), 'Fixture loop is not associated with the test image.')
    try:
        yield device
    finally:
        run('losetup', '--detach', device)


def wait_text(process, log, text, limit=180):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        if text in log.read_text(errors='replace'):
            return
        require(process.poll() is None, 'VM stopped before: ' + text)
        time.sleep(0.5)
    raise RuntimeError('VM timed out before: ' + text)


def ssh(port, key, *command):
    return run('ssh', '-i', key, '-p', port, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3',
        '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null', '-o', 'LogLevel=ERROR',
        'testadmin@127.0.0.1', *command, capture=True)


def wait_ssh(process, port, key, limit=180):
    end = time.monotonic() + limit
    while time.monotonic() < end:
        require(process.poll() is None, 'VM stopped before SSH was ready.')
        try:
            output = ssh(port, key, 'true')
            return
        except subprocess.CalledProcessError:
            time.sleep(2)
    raise RuntimeError('VM SSH timed out.')


@contextmanager
def guest(work, image, boot, extra, port, name):
    log = work / (name + '-console.log')
    command = ['qemu-system-aarch64', '-machine', 'virt', '-cpu', 'cortex-a53', '-m', '1024', '-smp', '2',
        '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-snapshot',
        '-kernel', str(boot / 'vmlinuz-6.12.111-current-rockchip64'),
        '-initrd', str(boot / 'initrd.img-6.12.111-current-rockchip64'),
        '-append', 'root=LABEL=OECT_ROOT rootfstype=ext4 ro console=ttyAMA0,115200 systemd.unified_cgroup_hierarchy=1 armhpc.mode=normal',
        '-drive', 'file=' + str(image) + ',format=raw,if=none,id=emmc', '-device', 'virtio-blk-device,drive=emmc',
        '-drive', 'file=' + str(extra) + ',format=raw,if=none,id=external', '-device', 'virtio-blk-device,drive=external',
        '-device', 'virtio-rng-device', '-netdev', 'user,id=net,hostfwd=tcp:127.0.0.1:' + str(port) + '-:22',
        '-device', 'virtio-net-device,netdev=net']
    with log.open('w') as stream:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=stream, stderr=subprocess.STDOUT, text=True)
        try:
            yield process, log
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('project', 'image', 'work-parent', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0 and sys.platform == 'linux', 'Verification requires Linux as root.')
    project = args.project.resolve(strict=True)
    image = args.image.resolve(strict=True)
    require(image.is_file() and not image.is_block_device(), 'Use a built disk image file.')
    metadata = json.loads(image.with_name(image.stem + '-build.json').read_text())
    require(digest(image) == metadata['image']['sha256'], 'Input image changed.')
    parent = args.work_parent.resolve(strict=True)
    work = parent / ('verify-rescue-' + str(os.getpid()))
    require(not work.exists(), 'Fixture work directory already exists.')
    work.mkdir()
    built_work = Path(metadata['work_directory'])
    boot, rescue_root = built_work / 'boot', built_work / 'rescue'
    module = load('armhpc_rescue_verify', project / 'components/rescue/rootfs-overlay/usr/lib/armhpc/rescue.py')
    administrator = load('armhpc_admin_verify', project / 'components/common/rootfs-overlay/usr/lib/armhpc/admin.py')
    module.firstrun_module = lambda: administrator
    module.FACTORY = rescue_root / 'usr/share/armhpc/factory'
    policy = json.loads((rescue_root / 'etc/armhpc/rescue.json').read_text())
    private_key = work / 'test-ed25519'
    run('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', private_key)
    key = private_key.with_suffix('.pub')
    sata = work / 'sata-70g.img'
    with sata.open('xb') as stream:
        stream.truncate(70 * 1024**3)
    checks = {}
    with loop_file(image, read_only=True) as emmc:
        for number, label in ((1, 'OECT_BOOT'), (2, 'OECT_RESCUE')):
            with module.mounted(emmc + 'p' + str(number), read_only=True) as root:
                require(run('blkid', '-s', 'LABEL', '-o', 'value', emmc + 'p' + str(number), capture=True) == label, 'Image label differs.')
                if number == 2:
                    for name in ('armhpc-rescue',):
                        require((root / 'usr/local/sbin' / name).stat().st_mode & 0o777 == 0o755, 'Rescue command is not executable.')
                    require(not (root / 'etc/image-framework/admin-provisioned.json').exists(), 'Image contains a node identity.')
                    require(not any((root / 'etc/ssh').glob('ssh_host_*')), 'Image contains SSH host keys.')
                    require((root / 'usr/share/armhpc/factory/factory-rootfs.tar.zst').stat().st_size == (module.FACTORY / 'factory-rootfs.tar.zst').stat().st_size, 'Factory archive is not embedded.')
                    for filename in ('usr/bin/sudo', 'usr/bin/ping'):
                        original, restored = rescue_root / filename, root / filename
                        if original.exists():
                            require(original.stat().st_mode == restored.stat().st_mode, 'Image file modes differ.')
                            for attribute in os.listxattr(original):
                                require(os.getxattr(original, attribute) == os.getxattr(restored, attribute), 'Image extended attributes differ.')
        checks['image_readonly_mount_metadata'] = True
    with loop_file(sata) as device:
        disk = {'path': device, 'size': sata.stat().st_size, 'serial': 'test-loop-only', 'number': 'fixture', 'model': 'fixture'}
        # Exercise the exact format/extract/provision code on the verified fixture loop.
        # Production CLI rejects loops and still requires Rescue, SATA and confirmation.
        module.do_install(disk, policy, 'testadmin', key)
        parts = module.validate_layout(disk, policy)
        before_table = run('sfdisk', '--dump', device, capture=True)
        data_uuid = run('blkid', '-s', 'UUID', '-o', 'value', parts[1]['node'], capture=True)
        sentinel = b'ARMHPC preserve-data verification\n'
        with module.mounted(parts[1]['node']) as data:
            file = data / 'sentinel.txt'
            file.write_bytes(sentinel)
            file.chmod(0o640)
            os.setxattr(file, 'user.armhpc', b'preserve')
            run('setfacl', '-m', 'u:1000:r--', file)
            before_acl = run('getfacl', '-cpn', file, capture=True)
        with module.mounted(parts[0]['node']) as root:
            (root / 'etc/machine-id').write_text('0a1b2c3d4e5f67890123456789abcdef0\n')
            (root / 'etc/hostname').write_text('armhpc-test-node\n')
            hosts = root / 'etc/hosts'
            hosts.write_text(hosts.read_text() + '\n127.0.1.1 armhpc-test-node\n')
            (root / 'root/should-disappear-after-repair').write_text('fixture')
            host_keys = {p.name: digest(p) for p in (root / 'etc/ssh').glob('ssh_host_*')}
        module.do_restore(disk, policy, 'testadmin', key)
        require(before_table == run('sfdisk', '--dump', device, capture=True), 'Repair changed GPT.')
        require(data_uuid == run('blkid', '-s', 'UUID', '-o', 'value', parts[1]['node'], capture=True), 'Repair changed DATA UUID.')
        with module.mounted(parts[1]['node'], read_only=True) as data:
            file = data / 'sentinel.txt'
            require(file.read_bytes() == sentinel and os.getxattr(file, 'user.armhpc') == b'preserve', 'Repair changed DATA content or xattrs.')
            require(run('getfacl', '-cpn', file, capture=True) == before_acl, 'Repair changed DATA ACLs.')
        with module.mounted(parts[0]['node'], read_only=True) as root:
            require(not (root / 'root/should-disappear-after-repair').exists(), 'Repair did not replace the system.')
            require((root / 'etc/machine-id').read_text().strip() == '0a1b2c3d4e5f67890123456789abcdef0', 'Machine identity changed during repair.')
            require((root / 'etc/hostname').read_text().strip() == 'armhpc-test-node', 'Hostname changed during repair.')
            require(host_keys == {p.name: digest(p) for p in (root / 'etc/ssh').glob('ssh_host_*')}, 'SSH host keys changed during repair.')
            for name in ('armhpc', 'armhpc-kube'):
                require((root / 'usr/local/sbin' / name).stat().st_mode & 0o777 == 0o755, 'Normal command is not executable.')
        checks['sata_install_64g_ext4'] = True
        checks['root_repair_preserves_gpt_data_acl_xattrs_and_identity'] = True
    keydisk = work / 'local-key-input.img'
    keydir = work / 'key-input'
    keydir.mkdir()
    shutil.copyfile(key, keydir / 'administrator.pub')
    with keydisk.open('xb') as stream:
        stream.truncate(8 * 1024**2)
    run('mkfs.ext4', '-F', '-m', '0', '-L', 'ARMHPC_TEST_KEY', '-d', keydir, keydisk)
    with guest(work, image, boot, keydisk, 22229, 'rescue') as (process, log):
        wait_text(process, log, 'ARMHPC Rescue:')
        process.stdin.write("armhpc-rescue status && echo ARMHPC_STATUS_OK\n")
        process.stdin.flush()
        wait_text(process, log, '\nARMHPC_STATUS_OK')
        process.stdin.write("mkdir -p /run/local-key; mount -t ext4 -o ro /dev/disk/by-label/ARMHPC_TEST_KEY /run/local-key && armhpc-rescue provision --username testadmin --public-key-file /run/local-key/administrator.pub && echo ARMHPC_PROVISION_OK\n")
        process.stdin.flush()
        wait_text(process, log, '\nARMHPC_PROVISION_OK')
        wait_ssh(process, 22229, private_key)
        help_text = ssh(22229, private_key, 'armhpc-rescue --help')
        status = ssh(22229, private_key, 'sudo -n armhpc-rescue status')
        require('armhpc-rescue' in help_text and 'restore' in help_text and 'Factory archives and BOOT files match' in status, 'Rescue SSH command output is incorrect.')
        checks['qemu_arm64_no_sata_falls_back_to_rescue'] = True
        checks['qemu_arm64_rescue_local_provision_and_ssh'] = True
        ssh(22229, private_key, 'sudo -n systemctl start armhpc-oect-boot-diagnostics.service')
        diagnostics = json.loads(ssh(22229, private_key, 'sudo -n cat /var/log/armhpc/boot-diagnostics-latest.json'))
        require(len(diagnostics['snapshots']) == 2, 'Rescue boot diagnostics did not save both observations.')
        require(diagnostics['snapshots'][-1]['network_state'] == 'carrier-present', 'Virtio Ethernet status is incorrect.')
        (work / 'rescue-boot-diagnostics.json').write_text(json.dumps(diagnostics, indent=2) + '\n')
        checks['qemu_arm64_rescue_boot_diagnostics'] = True
    with guest(work, image, boot, sata, 22230, 'normal') as (process, log):
        wait_text(process, log, 'ARMHPC: normal root')
        wait_ssh(process, 22230, private_key)
        details = ssh(22230, private_key, "set -e; uname -r; findmnt -rn --mountpoint / -o SOURCE,FSTYPE; findmnt -rn --mountpoint /data -o SOURCE,FSTYPE; findmnt -rn --mountpoint /boot -o SOURCE,FSTYPE; armhpc --help; sudo -n test -f /etc/armhpc/installation-complete.json")
        require('6.12.111-current-rockchip64' in details and 'armhpc-kube configuration' in details, 'Normal SSH command output is incorrect.')
        (work / 'normal-ssh.txt').write_text(details)
        checks['qemu_arm64_sata_root_and_ssh'] = True
    require(digest(image) == metadata['image']['sha256'], 'Verification modified the published image.')
    report = {'schema_version': 1, 'passed': True, 'image_sha256': metadata['image']['sha256'],
        'kernel_release': policy['kernel_release'], 'checks': checks, 'hardware_verified': False,
        'emulation': 'QEMU ARM64 virt, TCG, virtio disks/network; does not verify OECT DDR/U-Boot/eMMC/SATA controllers',
        'fixture_work_directory': str(work)}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
