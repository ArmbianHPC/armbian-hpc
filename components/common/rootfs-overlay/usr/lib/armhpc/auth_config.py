# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Normal-system SSH policy, with two new public-key sessions and timed rollback."""
import argparse
import base64
import getpass
import pwd
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import uuid
from config_io import (approve, atomic_write, configuration_lock, emit, entry, normal_system,
                       read_json, require, require_idle, run, safe_path, save_json)

STATE = 'var/lib/armhpc/auth/state.json'
MANAGED = 'etc/ssh/image-framework-admin.conf'
PENDING = ('key-check', 'policy-check')


def session():
    connection = os.environ.get('SSH_CONNECTION', '').split()
    require(len(connection) == 4, 'ssh_session', 'Use a new SSH connection for authentication confirmation')
    return ':'.join(connection)


def context():
    parts = os.environ.get('SSH_CONNECTION', '').split()
    require(len(parts) == 4, 'ssh_session', 'SSH connection context is missing')
    return 'user=' + pwd.getpwuid(os.geteuid()).pw_name + ',host=' + parts[0] + ',addr=' + parts[0] + ',laddr=' + parts[2] + ',lport=' + parts[3]


def shadow_digest(root=Path('/')):
    rows = [line.split(':') for line in (Path(root)/'etc/shadow').read_text().splitlines() if line.startswith('root:')]
    require(len(rows) == 1, 'auth_account', 'Expected one root password entry')
    return hashlib.sha256(rows[0][1].encode()).hexdigest()


def state(root=Path('/')):
    return read_json(safe_path(root, STATE), {'schema_version': 1, 'stage': 'idle', 'reminder_pending': False})


def effective():
    run('/usr/sbin/sshd', '-t')
    return dict(line.split(None, 1) for line in run('/usr/sbin/sshd', '-T', '-C', context()).splitlines() if ' ' in line)


def verify_key(record):
    require(session() != record['initiator_session'] and session() not in record.get('confirmed_sessions', []),
            'auth_new_session', 'Open a separate SSH connection; this session cannot confirm the change')
    authfile = os.environ.get('SSH_USER_AUTH')
    require(authfile, 'auth_method', 'Reconnect after ExposeAuthInfo was enabled and use public-key authentication')
    info = Path(authfile)
    require(not info.is_symlink() and info.is_file() and info.stat().st_uid == 0 and not info.stat().st_mode & 0o022 and info.stat().st_size <= 32768,
            'auth_method', 'Invalid sshd authentication evidence')
    from admin import parse_public_key
    matches = []
    for line in info.read_text().splitlines():
        if line.startswith('publickey '):
            _, fingerprint = parse_public_key(line.removeprefix('publickey '))
            matches.append(fingerprint)
    require(record['fingerprint'] in matches, 'auth_method', 'The new session did not authenticate with the selected public key')


def change_text(text, key_only=False):
    replacements = {'ExposeAuthInfo': 'yes'}
    if key_only:
        replacements.update(PasswordAuthentication='no', KbdInteractiveAuthentication='no',
                            AuthenticationMethods='publickey', PubkeyAuthentication='yes')
    lines = [line for line in text.splitlines() if not line.split() or line.split()[0] not in replacements]
    return '\n'.join(key + ' ' + value for key, value in replacements.items()) + '\n' + '\n'.join(lines) + '\n'


def save(record):
    save_json(safe_path(Path('/'), STATE), record)


def rollback(record):
    path = safe_path(Path('/'), MANAGED)
    current = hashlib.sha256(path.read_bytes()).hexdigest()
    require(current in record['managed_hashes'], 'auth_config_changed', 'SSH policy changed outside this transaction; automatic overwrite refused')
    atomic_write(path, base64.b64decode(record['backup']), 0o644)
    run('/usr/sbin/sshd', '-t')
    run('systemctl', 'reload', 'ssh.service')
    record.update(stage='rolled-back', reminder_pending=True)
    save(record)


def prepare(keyfile):
    normal_system(Path('/'))
    from node_state import data_mount
    data_mount()
    require_idle(Path('/'))
    old = state()
    require(old['stage'] not in PENDING, 'auth_pending', 'Confirm or roll back the existing authentication change')
    before = effective()
    require(before.get('usepam') == 'yes' and before.get('pubkeyauthentication') == 'yes',
            'auth_conflict', 'Resolve external PAM/public-key policy conflicts before applying')
    from admin import parse_public_key
    key, fingerprint = parse_public_key(Path(keyfile).read_text())
    authorized = safe_path(Path('/'), 'root/.ssh/authorized_keys')
    authorized.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(authorized.parent.stat().st_uid == 0 and not authorized.parent.stat().st_mode & 0o077,
            'auth_permissions', 'Root SSH directory must be private and root-owned')
    content = authorized.read_text() if authorized.exists() else ''
    if key.strip() not in [line.strip() for line in content.splitlines()]:
        atomic_write(authorized, content.rstrip() + '\n' + key, 0o600)
    path = safe_path(Path('/'), MANAGED)
    backup = path.read_bytes()
    proposed = change_text(backup.decode())
    record = {**old, 'schema_version': 1, 'transaction_id': str(uuid.uuid4()), 'stage': 'key-check',
              'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
              'deadline': time.clock_gettime(time.CLOCK_BOOTTIME) + 90, 'fingerprint': fingerprint,
              'initiator_session': session(), 'confirmed_sessions': [],
              'backup': base64.b64encode(backup).decode(),
              'managed_hashes': [hashlib.sha256(backup).hexdigest(), hashlib.sha256(proposed.encode()).hexdigest()]}
    save(record)
    try:
        atomic_write(path, proposed)
        require(effective().get('exposeauthinfo') == 'yes', 'auth_conflict', 'ExposeAuthInfo did not take effect')
        run('systemctl', 'reload', 'ssh.service')
        run('systemd-run', '--quiet', '--unit=armhpc-auth-' + record['transaction_id'],
            '--property=Type=exec', '--property=Restart=on-failure', '--property=RuntimeMaxSec=120',
            '/usr/bin/python3', Path(__file__).resolve(), '_watch', '--transaction', record['transaction_id'])
    except Exception:
        rollback(record)
        raise
    return public(record)


def public(record):
    return {key: value for key, value in record.items() if key not in ('backup', 'managed_hashes', 'password_digest')}


def confirm(identifier):
    record = state()
    require(record.get('transaction_id') == identifier and record['stage'] in PENDING,
            'auth_transaction', 'No matching pending authentication transaction')
    require(record['boot_id'] == Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            and time.clock_gettime(time.CLOCK_BOOTTIME) < record['deadline'], 'auth_expired', 'Confirmation expired; wait for rollback')
    verify_key(record)
    record['confirmed_sessions'].append(session())
    if record['stage'] == 'key-check':
        path = safe_path(Path('/'), MANAGED)
        require(hashlib.sha256(path.read_bytes()).hexdigest() in record['managed_hashes'],
                'auth_config_changed', 'SSH configuration changed during confirmation')
        proposed = change_text(path.read_text(), True)
        record['managed_hashes'].append(hashlib.sha256(proposed.encode()).hexdigest())
        record['stage'] = 'policy-check'
        save(record)
        try:
            atomic_write(path, proposed)
            actual = effective()
            require(actual.get('passwordauthentication') == 'no' and actual.get('kbdinteractiveauthentication') == 'no'
                    and actual.get('authenticationmethods') == 'publickey' and actual.get('usepam') == 'yes',
                    'auth_conflict', 'Effective SSH policy differs; external policy conflicts require review')
            run('systemctl', 'reload', 'ssh.service')
        except Exception:
            rollback(record)
            raise
    else:
        record.update(stage='confirmed', authentication='publickey', reminder_pending=False)
        save(record)
    return public(record)


def recover(watch=False, identifier=None):
    while True:
        with configuration_lock(Path('/')):
            record = state()
            if record['stage'] not in PENDING or identifier and record.get('transaction_id') != identifier:
                return
            if record.get('boot_id') != Path('/proc/sys/kernel/random/boot_id').read_text().strip() or time.clock_gettime(time.CLOCK_BOOTTIME) >= record['deadline']:
                rollback(record)
                return
        if not watch:
            return
        time.sleep(1)


def remind():
    if not os.environ.get('SSH_CONNECTION') or not sys.stdin.isatty() or not sys.stdout.isatty():
        return
    record = state()
    if record.get('reminder_pending'):
        print('\nK3s 已配置。请选择普通系统 SSH 登录策略：\n  1 修改密码\n  2 配置密钥并禁用密码登录\n  0 稍后处理', flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], 30)
        if not ready:
            return
        choice = sys.stdin.readline().strip()
        if choice == '1':
            with configuration_lock(Path('/')):
                require_idle(Path('/'))
                change_password()
        elif choice == '2':
            print('先将公钥上传到本机，再运行 armhpc auth key-only --public-key-file 文件。')


def change_password():
    from node_state import data_mount
    data_mount()
    require(state()['stage'] not in PENDING, 'auth_pending', 'Confirm or roll back the pending SSH change before modifying the password')
    before = shadow_digest()
    subprocess.run(['passwd', 'root'], check=True)
    require(shadow_digest() != before, 'auth_password', 'Password did not change')
    record = state()
    record.update(stage='configured', reminder_pending=False, password_digest=shadow_digest(), authentication='password-allowed')
    save(record)


def key_rows():
    from admin import parse_public_key
    path=safe_path(Path('/'),'root/.ssh/authorized_keys')
    rows=[]
    for line in path.read_text().splitlines() if path.is_file() else []:
        match=__import__('re').search(r'(?<!\S)(ssh-(?:ed25519|rsa)|ecdsa-sha2-\S+)\s+\S+',line)
        fingerprint=None
        if match:
            try:fingerprint=parse_public_key(line[match.start():])[1]
            except ValueError:pass
        rows.append((line,fingerprint))
    return path,rows


def manage_key(action,keyfile=None,fingerprint=None):
    from node_state import data_mount
    from admin import parse_public_key
    data_mount()
    require(state()['stage'] not in PENDING,'auth_pending','Finish the pending SSH change first')
    path,rows=key_rows()
    if action=='key-add':
        require(keyfile is not None,'public_key','Supply --public-key-file')
        key,fingerprint=parse_public_key(Path(keyfile).read_text())
        if fingerprint not in {p for _,p in rows}:rows.append((key.strip(),fingerprint))
    else:
        require(fingerprint and any(p==fingerprint for _,p in rows),'public_key','Select an existing key fingerprint')
        if state().get('authentication')=='publickey':
            require(any(p and p!=fingerprint for _,p in rows),'auth_last_key','Keep at least one working public key')
            evidence=Path(os.environ.get('SSH_USER_AUTH','/nonexistent'))
            require(evidence.is_file() and not evidence.is_symlink(),'auth_method','Remove a key from another verified public-key session')
            require(evidence.stat().st_uid==0 and not evidence.stat().st_mode&0o022,
                    'auth_method','SSH authentication evidence must not be writable by other users')
            proof=[parse_public_key(line.removeprefix('publickey '))[1] for line in evidence.read_text().splitlines() if line.startswith('publickey ')]
            require(proof and fingerprint not in proof,'auth_current_key','Reconnect with another key before removing the current key')
        rows=[(line,p) for line,p in rows if p!=fingerprint]
    path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
    require(path.parent.stat().st_uid==0 and not path.parent.stat().st_mode&0o077,'auth_permissions','Root SSH directory must be private')
    atomic_write(path,'\n'.join(line for line,_ in rows)+'\n',0o600)
    return {'fingerprints':[p for _,p in rows if p],'authentication_policy_changed':False}


def main():
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG', 'armhpc auth'))
    parser.add_argument('action', nargs='?', default='status', choices=('status','keys','key-add','key-remove','password','key-only','confirm','rollback','_recover','_watch'))
    parser.add_argument('--public-key-file', type=Path)
    parser.add_argument('--transaction')
    parser.add_argument('--fingerprint')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    normal_system(Path('/'))
    if args.action in ('_recover', '_watch'):
        recover(args.action == '_watch', args.transaction)
        return
    with configuration_lock(Path('/')):
        if args.action=='keys':
            result={'fingerprints':[p for _,p in key_rows()[1] if p]}
        elif args.action == 'status':
            result = public(state())
        else:
            require_idle(Path('/'))
            approve(args.yes, 'Apply the selected normal-system SSH authentication operation?')
            if args.action == 'password':
                change_password()
                result = public(state())
            elif args.action in ('key-add','key-remove'):
                result=manage_key(args.action,args.public_key_file,args.fingerprint)
            elif args.action == 'key-only':
                require(args.public_key_file is not None, 'public_key', 'Supply --public-key-file')
                result = prepare(args.public_key_file)
            elif args.action == 'confirm':
                result = confirm(args.transaction)
            else:
                record = state()
                require(args.transaction == record.get('transaction_id') and record['stage'] in PENDING,
                        'auth_transaction', 'Use the original pending transaction ID')
                rollback(record)
                result = public(record)
    emit(result, args.json)


if __name__ == '__main__':
    raise SystemExit(entry(main))
