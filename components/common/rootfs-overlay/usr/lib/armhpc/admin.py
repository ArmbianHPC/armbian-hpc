#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Provision an administrator in a mounted, offline ext4 root filesystem."""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import tempfile


class ProvisionError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ProvisionError(message)


def target_path(root, relative):
    parts = PurePosixPath(relative)
    require(not parts.is_absolute() and '..' not in parts.parts and '\\' not in relative,
            '目标路径必须留在离线根文件系统内。')
    path = root
    for part in parts.parts:
        path = path / part
        require(not path.is_symlink(), f'管理员配置路径不能是符号链接：{relative}')
    require(path.resolve().is_relative_to(root), '配置路径越过目标根文件系统。')
    return path


def validate_username(value):
    require(re.fullmatch(r'[a-z][a-z0-9_-]{0,30}', value) is not None
            and value not in ('root', 'nobody'), '管理员名称应为小写 Linux 用户名，不能使用 root。')


def parse_public_key(content):
    """Accept one plain public key; exclude authorized_keys options and secrets."""
    require(len(content) <= 16384 and '\x00' not in content, 'SSH 公钥文件无效。')
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    require(len(lines) == 1 and 'PRIVATE KEY' not in content, '文件必须只包含一行 SSH 公钥。')
    fields = lines[0].split()
    allowed = {'ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256',
               'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521',
               'sk-ssh-ed25519@openssh.com', 'sk-ecdsa-sha2-nistp256@openssh.com'}
    require(len(fields) >= 2 and fields[0] in allowed, '请提供不带 authorized_keys 选项的 SSH 公钥。')
    try:
        blob = base64.b64decode(fields[1], validate=True)
        length = struct.unpack('>I', blob[:4])[0]
        algorithm = blob[4:4 + length].decode('ascii')
    except (ValueError, UnicodeError, struct.error) as exc:
        raise ProvisionError('SSH 公钥编码无效。') from exc
    require(algorithm == fields[0] and 4 + length < len(blob), 'SSH 公钥类型与编码不一致。')
    fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
    # Strip comments to keep installer-local names out of the installed metadata.
    return fields[0] + ' ' + fields[1] + '\n', fingerprint


def atomic_write(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def in_target(root, *args):
    # An offline archive has an empty /dev. Supply devices only for the command,
    # without creating device nodes or leaving mounts in the installed rootfs.
    created_mount = False
    device_directory = None
    if root != Path('/'):
        device_directory = target_path(root, 'dev')
        device_directory.mkdir(exist_ok=True)
        mounted = subprocess.run(['mountpoint', '-q', str(device_directory)])
        if mounted.returncode != 0:
            subprocess.run(['mount', '--bind', '/dev', str(device_directory)], check=True)
            created_mount = True
    try:
        result = subprocess.run(['chroot', str(root), *args], text=True,
                                capture_output=True, timeout=60,
                                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        require(result.returncode == 0, f'目标系统中的 {Path(args[0]).name} 执行失败；SSH 仍保持关闭。')
        return result.stdout
    finally:
        if created_mount:
            subprocess.run(['umount', str(device_directory)], check=True)


def check_offline_root(root):
    require(sys.platform == 'linux' and os.geteuid() == 0, '此工具需要在 Linux 安装或 Rescue 环境中以 root 运行。')
    require(root.is_dir() and root != Path('/'), '必须指定已挂载的离线系统目录，不能使用运行中的 /。')
    require(not os.path.samefile(root, '/'), '目标是运行中根目录的绑定挂载，不能作为离线系统配置。')
    result = subprocess.run(['findmnt', '-rn', '--mountpoint', str(root), '-o', 'FSTYPE'],
                            text=True, capture_output=True, timeout=10)
    require(result.returncode == 0 and result.stdout.strip() == 'ext4', '目标必须是独立挂载的 ext4 根文件系统。')
    for name in ('etc/passwd', 'etc/shadow', 'etc/group', 'etc/ssh/sshd_config',
                 'usr/sbin/useradd', 'usr/sbin/usermod', 'usr/sbin/sshd',
                 'usr/sbin/visudo', 'usr/bin/ssh-keygen'):
        require(target_path(root, name).is_file(), f'目标根文件系统缺少 {name}。')
    target_path(root, 'home')


def account(root, username):
    entries = []
    for line in target_path(root, 'etc/passwd').read_text().splitlines():
        fields = line.split(':')
        if fields[0] == username:
            require(len(fields) == 7, '目标 passwd 记录无效。')
            entries.append(fields)
    require(len(entries) <= 1, '目标中有重复管理员记录。')
    return entries[0] if entries else None


def check_rescue_root(root):
    require(sys.platform == 'linux' and os.geteuid() == 0 and root == Path('/'),
            'Rescue 管理员只在本机 root 终端初始化。')
    require(sys.stdin.isatty() and not os.environ.get('SSH_CONNECTION')
            and Path('/etc/armhpc/rescue-environment').is_file(),
            'Rescue 管理员需要本地交互终端，不能经 SSH 初始化。')
    policy = json.loads(Path('/etc/armhpc/rescue.json').read_text())
    mounted = subprocess.run(['findmnt', '-rn', '--mountpoint', '/', '-o', 'SOURCE,FSTYPE'],
                             text=True, capture_output=True, check=True).stdout.split()
    require(len(mounted) == 2 and mounted[1] == 'ext4', 'Rescue 根目录必须为 ext4。')
    label = subprocess.run(['blkid', '-s', 'LABEL', '-o', 'value', mounted[0]],
                           text=True, capture_output=True, check=True).stdout.strip()
    require(label == policy['rescue_label'], '运行中的根分区不是已配置的 Rescue。')
    for unit in ('ssh.service', 'ssh.socket'):
        require(subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode != 0,
                '初始化 Rescue 管理员时 SSH 必须尚未运行。')


def provision(root, username, public_key_file, *, rescue=False):
    root = Path(root).resolve()
    validate_username(username)
    if rescue:
        check_rescue_root(root)
    else:
        check_offline_root(root)
    key, fingerprint = parse_public_key(Path(public_key_file).read_text(encoding='utf-8'))
    marker = target_path(root, 'etc/image-framework/admin-provisioned.json')
    pending_path = target_path(root, 'etc/image-framework/admin-provisioning.json')
    previous = json.loads(marker.read_text()) if marker.exists() else None
    pending = json.loads(pending_path.read_text()) if pending_path.exists() else None
    if previous:
        require(previous.get('username') == username or previous.get('authentication') == 'root_password',
                '此系统已配置另一位管理员。')
    if pending:
        require(pending.get('username') == username, '此系统有另一位管理员的未完成安装。')
    existing = account(root, username)
    require(not existing or previous or pending, '此用户名已属于现有账户，请选择其他管理员名称。')
    # Fail closed even when reprovisioning is interrupted.
    marker.unlink(missing_ok=True)
    for unit in ('ssh.service', 'ssh.socket'):
        atomic_write(target_path(root, f'etc/systemd/system/{unit}.d/10-image-framework-admin.conf'),
                     '[Unit]\nConditionPathExists=/etc/image-framework/admin-provisioned.json\n', 0o644)
    atomic_write(pending_path, json.dumps({'username': username}) + '\n', 0o600)
    if not existing:
        in_target(root, '/usr/sbin/useradd', '--create-home', '--user-group', '--groups', 'sudo',
                  '--shell', '/bin/bash', '--password', '!', username)
    else:
        in_target(root, '/usr/sbin/usermod', '--append', '--groups', 'sudo', '--lock', username)
    entry = account(root, username)
    require(entry is not None and int(entry[2]) >= 1000 and int(entry[3]) > 0
            and entry[5] == '/home/' + username, '管理员必须是拥有独立 home 的普通用户。')
    uid, gid = int(entry[2]), int(entry[3])
    key_directory = target_path(root, 'home/' + username + '/.ssh')
    key_directory.mkdir(parents=True, exist_ok=True)
    key_directory.chmod(0o700)
    os.chown(key_directory, uid, gid)
    key_path = target_path(root, 'home/' + username + '/.ssh/authorized_keys')
    atomic_write(key_path, key, 0o600)
    os.chown(key_path, uid, gid)
    in_target(root, '/usr/bin/ssh-keygen', '-l', '-f', '/home/' + username + '/.ssh/authorized_keys')
    # This account has no password. Sudo uses its SSH-key-authenticated session.
    sudoers = target_path(root, 'etc/sudoers.d/90-image-framework-admin')
    atomic_write(sudoers, username + ' ALL=(ALL:ALL) NOPASSWD: ALL\n', 0o440)
    in_target(root, '/usr/sbin/visudo', '-c')
    config = target_path(root, 'etc/ssh/sshd_config')
    previous_config = config.read_text()
    include = 'Include /etc/ssh/image-framework-admin.conf'
    atomic_write(config, include + '\n' + '\n'.join(line for line in previous_config.splitlines()
                                                    if line.strip() != include) + '\n', 0o644)
    atomic_write(target_path(root, 'etc/ssh/image-framework-admin.conf'),
                 'PermitRootLogin no\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n'
                 'PubkeyAuthentication yes\nAuthenticationMethods publickey\nUsePAM yes\n'
                 'AuthorizedKeysFile .ssh/authorized_keys\nAllowUsers ' + username + '\n', 0o644)
    # Remove Armbian's generic first-login account/password setup trigger.
    target_path(root, 'root/.not_logged_in_yet').unlink(missing_ok=True)
    target_path(root, 'run/sshd').mkdir(parents=True, exist_ok=True)
    if not previous:
        for host_key in target_path(root, 'etc/ssh').glob('ssh_host_*_key*'):
            target_path(root, host_key.relative_to(root).as_posix()).unlink()
    in_target(root, '/usr/bin/ssh-keygen', '-A')
    effective = in_target(root, '/usr/sbin/sshd', '-T', '-C',
                          f'user={username},host=localhost,addr=127.0.0.1')
    values = dict(line.split(' ', 1) for line in effective.splitlines() if ' ' in line)
    required = {'permitrootlogin': 'no', 'passwordauthentication': 'no',
                'kbdinteractiveauthentication': 'no', 'pubkeyauthentication': 'yes',
                'authenticationmethods': 'publickey', 'usepam': 'yes',
                'authorizedkeysfile': '.ssh/authorized_keys', 'allowusers': username}
    require(all(values.get(name) == value for name, value in required.items()),
            'SSH 有效配置与公钥登录策略不一致，请检查目标配置的 Include/Match。')
    in_target(root, '/usr/sbin/sshd', '-t')
    receipt = {'schema_version': 1, 'username': username, 'key_fingerprint': fingerprint,
               'authentication': 'ssh_public_key', 'sudo': 'passwordless',
               'created_utc': datetime.now(timezone.utc).isoformat()}
    atomic_write(marker, json.dumps(receipt, indent=2) + '\n', 0o644)
    atomic_write(target_path(root, 'etc/armhpc/authentication.json'),
                 json.dumps({'authentication': 'ssh_public_key', 'username': username}) + '\n', 0o644)
    pending_path.unlink()
    print(f'离线系统管理员已配置：{username}。公钥指纹：{fingerprint}')
    return receipt


def main():
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG'), description='在本地安装阶段创建管理员并导入 SSH 公钥。')
    parser.add_argument('--rootfs', type=Path, required=True, help='独立挂载的离线 ext4 根目录。')
    parser.add_argument('--username', required=True, help='需要创建的管理员用户名。')
    parser.add_argument('--public-key-file', type=Path, required=True, help='本地的一行 SSH 公钥文件。')
    args = parser.parse_args()
    provision(args.rootfs, args.username, args.public_key_file)


if __name__ == '__main__':
    try:
        main()
    except (ProvisionError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'管理员配置未完成：{exc}', file=sys.stderr)
        sys.exit(1)
