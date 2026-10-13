#!/usr/bin/python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Archive node-local K3s state and return to the installation wizard."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid

OPERATIONS = ('uninstall', 'reinstall', 'clear')
UNITS = ('k3s', 'k3s-agent')


class ManageError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code, message):
    if not condition:
        raise ManageError(code, message)


def sync_parent(path):
    fd = os.open(Path(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value.encode() if isinstance(value, str) else value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_parent(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def read(path, default=None):
    if not Path(path).is_file():
        return default
    value = json.loads(Path(path).read_text())
    require(isinstance(value, dict), 'record', 'Configuration record must be an object')
    return value


def command(*args, timeout=60, env=None):
    return subprocess.run(list(map(str, args)), text=True, capture_output=True, timeout=timeout,
                          env={**os.environ, 'LC_ALL': 'C', **(env or {})})


class Manager:
    def __init__(self, root=Path('/'), runner=command):
        self.root = Path(root).resolve()
        self.run = runner
        self.policy_file = self.path('/etc/image-framework/k3s.json')
        self.policy = read(self.policy_file)
        require(self.policy is not None, 'policy', 'This system has no ARMHPC K3s policy')
        self.validate_policy()
        self.task_file = self.path(self.policy['backups_dir'] + '/k3s-management.json')

    def path(self, absolute):
        value = Path(absolute)
        require(value.is_absolute() and '..' not in value.parts, 'path', 'Expected an absolute managed path')
        target = self.root / str(value).lstrip('/')
        require(not any(p.is_symlink() for p in (target, *target.parents)
                        if p != self.root and p.is_relative_to(self.root)),
                'path', 'A managed path or parent is a symbolic link: ' + str(value))
        return target

    def validate_policy(self):
        mount = self.policy['data_mountpoint']
        require(mount not in ('/', '/boot', '/etc', '/usr', '/var', '/run', '/dev', '/proc', '/sys')
                and not any(c.isspace() for c in mount), 'policy', 'Invalid DATA mountpoint')
        expected = {'data_dir': mount + '/k3s', 'configuration_dir': mount + '/node-config/k3s',
                    'state_file': mount + '/node-config/k3s/setup-state.json',
                    'kubelet_source': mount + '/kubelet', 'kubelet_target': '/var/lib/kubelet',
                    'node_identity_source': mount + '/node-config/k3s/node',
                    'node_identity_target': '/etc/rancher/node', 'local_pv_dir': mount + '/pv',
                    'backups_dir': mount + '/backups',
                    'completion_marker': '/var/lib/image-framework/k3s/setup-complete'}
        require(all(self.policy.get(k) == v for k, v in expected.items()),
                'policy', 'K3s paths differ from the managed ARMHPC layout')
        for value in (mount, *expected.values()):
            self.path(value)
        require(not self.path('/etc/armhpc/rescue-environment').is_file(),
                'mode', 'Run K3s management in the normal system')
        result = self.run('findmnt', '-rn', '-M', mount, '-o', 'TARGET,FSTYPE,LABEL')
        require(result.returncode == 0 and result.stdout.split() ==
                [mount, self.policy['data_filesystem'], self.policy['data_label']],
                'data_mount', 'DATA must be the expected independently mounted filesystem')

    def units(self):
        values = {}
        for name in UNITS:
            result = self.run('systemctl', 'show', name, '--property=LoadState,ActiveState,SubState,MainPID')
            values[name] = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        extra = [p.name for p in self.path('/etc/systemd/system').glob('k3s*.service')
                 if p.name not in ('k3s.service', 'k3s-agent.service')]
        require(not extra, 'units', 'Custom K3s units require separate handling')
        return values

    def check_bindings(self):
        for source, target in (('kubelet_source', 'kubelet_target'),
                               ('node_identity_source', 'node_identity_target')):
            actual = self.path(self.policy[target])
            if self.run('mountpoint', '-q', actual).returncode == 0:
                require(os.path.samefile(actual, self.path(self.policy[source])),
                        'binding', 'A managed bind mount has an unexpected source')

    def plan(self, operation, version=None):
        version = version or self.policy['default_version']
        require(re.fullmatch(r'v\d+\.\d+\.\d+\+k3s\d+', version), 'version', 'Specify the complete K3s version')
        self.check_bindings()
        state = read(self.path(self.policy['state_file']), {})
        units = self.units()
        pending = read(self.task_file, {})
        if operation == 'clear':
            require(state.get('status') != 'installed' and
                    not self.path(self.policy['completion_marker']).exists() and
                    all(v.get('ActiveState') not in ('active', 'activating', 'reloading') for v in units.values()),
                    'installed', 'clear only handles unfinished installations; use reinstall for an installed node')
            if pending.get('stage') != 'incomplete':
                empty = not state and not self.path('/usr/local/bin/k3s').exists() and all(
                    not self.path(self.policy[k]).exists() or not any(p.is_file() or p.is_symlink()
                        for p in self.path(self.policy[k]).rglob('*'))
                    for k in ('data_dir', 'configuration_dir', 'kubelet_source', 'local_pv_dir'))
                require(state.get('status') in ('planned', 'installing', 'starting', 'failed') or empty,
                        'unknown_installation', 'Unrecorded K3s data requires an explicit uninstall')
        return {'operation': operation, 'hostname': socket.gethostname(),
                'previous_status': state.get('status'), 'previous_role': state.get('plan', {}).get('role'),
                'previous_version': state.get('plan', {}).get('version'), 'next_default_version': version,
                'units': units, 'archive_directories': [self.policy[k] for k in
                    ('data_dir', 'configuration_dir', 'kubelet_source', 'local_pv_dir')],
                'data_mountpoint': self.policy['data_mountpoint'], 'backup_parent': self.policy['backups_dir'],
                'stage': 'planned', 'stops_workloads': True}

    def persist(self, task):
        save(self.task_file, task)
        save(Path(task['backup']) / 'manifest.json', task)

    def checkpoint(self, task, name):
        if name not in task['completed']:
            task['completed'].append(name)
        self.persist(task)

    def logged(self, task, *args):
        result = self.run(*args, timeout=180, env={'K3S_DATA_DIR': str(self.path(self.policy['data_dir']))})
        log = Path(task['backup']) / 'commands.log'
        with log.open('a') as stream:
            log.chmod(0o600)
            stream.write(str(args[0]) + '\n' + result.stdout + result.stderr + '\n')
        require(result.returncode == 0, 'command', 'K3s cleanup failed; inspect the private backup commands.log')

    def snapshot(self, task):
        required = self.path('/usr/local/bin/k3s')
        needed = (required.stat().st_size if required.is_file() else 0) + 32 * 1024**2
        require(shutil.disk_usage(task['backup']).free >= needed,
                'backup_space', 'Insufficient DATA space for the private configuration/program backup')
        relative_files = ['/etc/image-framework/k3s.json', '/etc/fstab',
                          '/etc/rancher/k3s', '/etc/rancher/node',
                          self.policy['completion_marker'], '/etc/sysctl.d/90-k3s.conf']
        for unit in UNITS:
            relative_files += ['/etc/systemd/system/' + unit + suffix for suffix in
                               ('.service', '.service.env', '.service.d')]
        for name in ('k3s', 'k3s-killall.sh', 'k3s-uninstall.sh', 'k3s-agent-uninstall.sh'):
            relative_files.append('/usr/local/bin/' + name)
        for relative in relative_files:
            source = self.path(relative)
            if not source.exists():
                continue
            dest = Path(task['backup']) / 'root-files' / relative.lstrip('/')
            dest.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(source, dest)
        os.sync()

    def archive_data(self, task):
        mounts = self.run('findmnt', '-rn', '-o', 'TARGET')
        require(mounts.returncode == 0, 'mounts', 'Cannot inspect remaining mounts')
        for relative in task['archive_directories']:
            require(not any(p == relative or p.startswith(relative + '/') for p in mounts.stdout.splitlines()),
                    'mounts', 'A K3s data directory still contains a mounted filesystem: ' + relative)
            source = self.path(relative)
            dest = Path(task['backup']) / 'data' / relative.lstrip('/')
            name = 'archived:' + relative
            if name in task['completed']:
                continue
            require(not (source.exists() and dest.exists()), 'archive', 'Both archive and source exist; inspect before retrying')
            if source.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                require(source.stat().st_dev == dest.parent.stat().st_dev, 'archive', 'Archive must stay on the DATA filesystem')
                os.rename(source, dest)
                sync_parent(source.parent)
                sync_parent(dest.parent)
            self.checkpoint(task, name)

    def clear_bind_entries(self):
        path = self.path('/etc/fstab')
        rows = []
        expected = {self.policy['kubelet_target']: self.policy['kubelet_source'],
                    self.policy['node_identity_target']: self.policy['node_identity_source']}
        for row in path.read_text().splitlines():
            fields = row.split()
            if fields and not row.lstrip().startswith('#') and len(fields) > 1 and fields[1] in expected:
                require(fields[0] == expected[fields[1]] and len(fields) >= 4 and 'bind' in fields[3].split(','),
                        'fstab', 'An unexpected bind entry must be handled manually')
                continue
            rows.append(row)
        atomic(path, '\n'.join(rows) + '\n', path.stat().st_mode & 0o777)

    def execute(self, plan):
        task = read(self.task_file)
        if task and task.get('stage') == 'incomplete':
            require(task['operation'] == plan['operation'], 'resume', 'Continue the original incomplete K3s management operation')
            backup = Path(task['backup'])
            require(backup.resolve().parent == self.path(self.policy['backups_dir']).resolve() and
                    backup.name.startswith('k3s-reset-') and not backup.is_symlink() and
                    task['archive_directories'] == plan['archive_directories'],
                    'resume', 'The saved archive path or DATA layout changed')
        else:
            parent = self.path(self.policy['backups_dir'])
            parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = parent / ('k3s-reset-' + stamp + '-' + uuid.uuid4().hex[:8])
            backup.mkdir(mode=0o700)
            task = {**plan, 'backup': str(backup), 'completed': [], 'stage': 'incomplete'}
            self.persist(task)
        try:
            if 'stopped' not in task['completed']:
                for unit, value in self.units().items():
                    if value.get('LoadState') == 'loaded':
                        self.logged(task, 'systemctl', 'stop', unit)
                helper = self.path('/usr/local/bin/k3s-killall.sh')
                if helper.is_file():
                    require(helper.stat().st_uid == 0 and not helper.stat().st_mode & 0o022,
                            'helper', 'K3s killall helper must be owned by root and not writable by other users')
                    self.logged(task, 'sh', helper)
                for key in ('kubelet_target', 'node_identity_target'):
                    target = self.path(self.policy[key])
                    if self.run('mountpoint', '-q', target).returncode == 0:
                        self.logged(task, 'umount', target)
                require(all(int(v.get('MainPID', '0')) == 0 for v in self.units().values()),
                        'running', 'K3s is still running')
                self.checkpoint(task, 'stopped')
            if 'snapshot' not in task['completed']:
                self.snapshot(task)
                self.checkpoint(task, 'snapshot')
            self.archive_data(task)
            for name in ('k3s-agent-uninstall.sh', 'k3s-uninstall.sh'):
                step = 'uninstalled:' + name
                if step in task['completed']:
                    continue
                script = Path(task['backup']) / 'root-files/usr/local/bin' / name
                if script.is_file():
                    helper = self.path('/usr/local/bin/k3s-killall.sh')
                    saved = Path(task['backup']) / 'root-files/usr/local/bin/k3s-killall.sh'
                    if not helper.exists() and saved.is_file():
                        shutil.copy2(saved, helper)
                    self.logged(task, 'sh', script)
                self.checkpoint(task, step)
            require(not self.path('/usr/local/bin/k3s').exists(), 'uninstall', 'K3s binary remains; inspect the backed-up uninstall scripts')
            self.clear_bind_entries()
            for relative in ('/etc/systemd/system/k3s.service.d', '/etc/systemd/system/k3s-agent.service.d',
                             '/etc/rancher/node', '/etc/sysctl.d/90-k3s.conf', self.policy['completion_marker']):
                path = self.path(relative)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            for key in ('data_dir', 'configuration_dir', 'kubelet_source', 'local_pv_dir', 'node_identity_source'):
                self.path(self.policy[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
            self.policy['default_version'] = task['next_default_version']
            atomic(self.policy_file, json.dumps(self.policy, ensure_ascii=False, indent=2) + '\n',
                   self.policy_file.stat().st_mode & 0o777)
            self.logged(task, 'systemctl', 'daemon-reload')
            require(not self.path(self.policy['state_file']).exists() and
                    not any(self.path(self.policy['data_dir']).iterdir()), 'reset', 'Installation state is not empty')
            task['stage'] = 'completed'
            task['next_command'] = 'armhpc-kube configuration'
            self.persist(task)
            os.sync()
            return task
        except Exception as error:
            task['error'] = {'code': getattr(error, 'code', 'io'), 'message': str(error)}
            self.persist(task)
            raise


@contextmanager
def live_lock():
    from config_io import configuration_lock, require_idle
    with configuration_lock(Path('/')):
        require_idle(Path('/'))
        with open('/run/image-framework-armhpc-k3s-configuration.lock', 'a+b') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ManageError('busy', 'Another K3s installation wizard is running') from error
            yield


def main(argv=None):
    prog = globals().get('ARMHPC_PROG', 'armhpc-kube manage')
    chosen = prog.rsplit(' ', 1)[-1]
    parser = argparse.ArgumentParser(prog=prog, description='备份并卸载 K3s、重新进入安装向导，或清理未完成安装。')
    if chosen not in OPERATIONS:
        parser.add_argument('operation', choices=OPERATIONS)
    else:
        parser.set_defaults(operation=chosen)
    parser.add_argument('--preview', action='store_true', help='只预览操作和备份路径。')
    parser.add_argument('--yes', action='store_true', help='已确认停止此节点 K3s 并归档本地集群数据。')
    parser.add_argument('--json', action='store_true', help='输出 protocol 1 JSON。')
    parser.add_argument('--k3s-version', dest='version', help='重置后安装向导默认使用的完整 K3s 版本。')
    args = parser.parse_args(argv)
    try:
        require(sys.platform == 'linux' and os.geteuid() == 0, 'root', 'Run as root in the normal Linux system')
        if args.operation == 'reinstall' and not args.preview:
            require(sys.stdin.isatty() and sys.stdout.isatty() and not args.json,
                    'terminal', 'reinstall requires an interactive SSH terminal; use uninstall for a batch reset')
        with live_lock():
            manager = Manager()
            plan = manager.plan(args.operation, args.version)
            if args.preview:
                value = plan
            else:
                if not args.yes:
                    require(sys.stdin.isatty(), 'confirmation', 'Use --yes after reviewing the reset plan')
                    print(json.dumps(plan, ensure_ascii=False, indent=2))
                    require(input('将停止此节点全部 K3s 工作负载并归档数据；请输入本机主机名确认：').strip() == plan['hostname'],
                            'cancelled', 'K3s management cancelled')
                value = manager.execute(plan)
        if args.json:
            print(json.dumps({'protocol': 1, 'ok': True, 'data': value}, ensure_ascii=False))
        else:
            print(json.dumps(value, ensure_ascii=False, indent=2))
        if args.operation == 'reinstall' and not args.preview:
            os.execv('/usr/local/sbin/armhpc-kube', ['armhpc-kube', 'configuration'])
        return 0
    except (ManageError, OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
        print(json.dumps({'protocol': 1, 'ok': False, 'error': {
            'code': getattr(error, 'code', 'io'), 'message': str(error)}}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
