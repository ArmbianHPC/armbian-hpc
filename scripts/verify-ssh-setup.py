# SPDX-License-Identifier: GPL-2.0-only
"""Verify setup decisions without installing software or changing a host."""

import ast
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from framework.config import ARCHITECTURES, resolve

OVERLAY = ROOT / 'components/common/rootfs-overlay'
SETUP = ROOT / 'components/k3s/rootfs-overlay/usr/lib/armhpc/kube_setup.py'
PREFLIGHT = ROOT / 'components/k3s/rootfs-overlay/usr/lib/armhpc/kube_check.py'
POLICY = resolve(ROOT, 'wxy-oect-k3s', require_sources=False)['setup_policy']
TEST_TOKEN = 'K10test-only-credential::not-a-real-token'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def namespace(path):
    return runpy.run_path(str(path), run_name='framework_verify')['main'].__globals__


def expect_rejection(function, value):
    try:
        function(value)
    except (ValueError, RuntimeError):
        return
    raise AssertionError(f'Expected rejection: {value!r}')


def verify_inputs(setup):
    for value in ('stable', 'v1.36.5+k3s1; id', '$(id)', ''):
        expect_rejection(setup['validate_version'], value)
    for value in ('UPPERCASE', 'node_01', '-node', 'node;id', ''):
        expect_rejection(setup['validate_name'], value)
    for value in (
        'http://server:6443', 'https://user:secret@server:6443',
        'https://server:6443/?token=secret', 'https://server:6443/other',
        'https://server:99999', 'https://server\n:6443', 'https://server:6443; id',
    ):
        expect_rejection(setup['validate_server'], value)
    for value in ('https://server.lan:6443', 'https://192.168.1.10:6443', 'https://[2001:db8::1]:6443'):
        setup['validate_server'](value)
    setup['validate_version'](POLICY['default_version'])
    for value in ('http://mirror.example', 'https://user:pass@mirror.example',
                  'https://mirror.example/?token=x', 'https://mirror.example/#fragment',
                  'https://mirror.example:99999', 'https://mirror.example/\nscript'):
        expect_rejection(setup['validate_download_url'], value)


def verify_wizard(setup):
    plans = []
    for role, expected in [('1', 'server-single'), ('2', 'server-init'), ('3', 'agent'), ('4', 'server-join')]:
        answers = [role, 'oect-test', '']
        if role in ('3', '4'):
            answers.append('https://server.lan:6443')
        if role != '3':
            answers += [''] * 7
        answers += ['1', 'n']
        output = io.StringIO()
        with mock.patch('builtins.input', side_effect=answers), mock.patch.object(setup['getpass'], 'getpass', return_value=TEST_TOKEN), contextlib.redirect_stdout(output):
            plan, token = setup['collect_plan'](POLICY)
        require(plan['role'] == expected, 'Role selection failed.')
        require(TEST_TOKEN not in output.getvalue(), 'Token appeared in terminal output.')
        require(TEST_TOKEN not in json.dumps(plan), 'Token appeared in state metadata.')
        configuration = setup['config_text'](POLICY, plan)
        require(TEST_TOKEN not in configuration, 'Token appeared in K3s YAML.')
        require('data-dir: "/data/k3s"' in configuration, 'K3s data is not on SATA data partition.')
        if role in ('3', '4'):
            require(token == TEST_TOKEN and 'token-file:' in configuration, 'Join credentials are not separated.')
        else:
            require(token is None and 'token-file:' not in configuration, 'New cluster requests join credentials.')
        require(('cluster-init: true' in configuration) == (role == '2'), 'Only the first etcd server may initialize the cluster.')
        plans.append(plan)
    with mock.patch('builtins.input', return_value='0'), contextlib.redirect_stdout(io.StringIO()):
        require(setup['collect_plan'](POLICY) == (None, None), 'Cancel did not exit setup.')
    return plans


def verify_online_pipeline(setup, plan, policy=POLICY):
    for fail_installer, fail_service in ((False, False), (True, False), (False, True)):
        states, writes, commands = [], [], []

        def fake_run(args, **kwargs):
            commands.append((args, kwargs))
            if args[0] == 'sh' and fail_installer:
                raise setup['SetupError']('Simulated download/install failure.')
            if args[:2] == ['systemctl', 'show']:
                return 'failed' if fail_service else 'active'
            return ''

        state = {'operation_id': 'verification-only', 'status': 'planned', 'plan': plan}
        patches = {
            'save_state': lambda policy, value: states.append(value['status']),
            'private_write': lambda path, text, *args: writes.append((str(path), text)),
            'prepare_storage': lambda policy, value: 'k3s',
            'download_installer': lambda policy, path: None,
            'run': fake_run,
        }
        with mock.patch.dict(setup, patches), mock.patch.object(setup['tempfile'], 'TemporaryDirectory', return_value=contextlib.nullcontext(str(ROOT))), contextlib.redirect_stdout(io.StringIO()):
            try:
                setup['install'](policy, state, None)
            except setup['SetupError']:
                require(fail_installer or fail_service, 'Unexpected pipeline failure.')
        marked = any(path == policy['completion_marker'] for path, _ in writes)
        if fail_installer:
            require(states == ['installing'] and not marked, 'Failed installer marked setup complete.')
            require(not any(args[0] == 'systemctl' for args, _ in commands), 'Failed installer started K3s.')
        elif fail_service:
            require(states == ['installing', 'starting'] and not marked, 'Failed service marked setup complete.')
        else:
            require(states == ['installing', 'starting', 'installed'] and marked, 'Completion occurred before service checks.')
            installer_calls = [kwargs for args, kwargs in commands if args[0] == 'sh']
            environment = installer_calls[0]['env']
            require(environment['INSTALL_K3S_VERSION'] == policy['default_version'], 'Version is not pinned.')
            require(environment['INSTALL_K3S_SKIP_START'] == 'true', 'Installer bypasses the storage gate.')
            require(environment['ARCH'] == policy['installer_arch'], 'Installer architecture is not selected by the target.')
            require(environment['INSTALL_K3S_ARTIFACT_URL'] == plan['download_source']['artifact_url'], 'Selected release source was lost.')
            require('INSTALL_K3S_SKIP_DOWNLOAD' not in environment, 'Online setup uses offline installation.')


def verify_preflight():
    gate = namespace(PREFLIGHT)
    cases = [
        ('/data ext4 OECT_DATA', 0, True, 'installed', True),
        ('/data ext4 OECT_DATA', 0, True, 'starting', True),
        ('/ ext4 OECT_DATA', 0, True, 'installed', False),
        ('/data xfs OECT_DATA', 0, True, 'installed', False),
        ('/data ext4 WRONG', 0, True, 'installed', False),
        ('/data ext4 OECT_DATA', 1, True, 'installed', False),
        ('/data ext4 OECT_DATA', 0, False, 'installed', False),
        ('/data ext4 OECT_DATA', 0, True, 'failed', False),
        ('/data ext4 OECT_DATA', 0, True, 'installing', False),
    ]
    for mount, mount_code, same, status, expected in cases:
        def fake_path(path):
            value = POLICY if path == '/etc/image-framework/k3s.json' else {'status': status}
            return SimpleNamespace(read_text=lambda: json.dumps(value))

        accepted = True
        with mock.patch.dict(gate, {'Path': fake_path}), mock.patch.object(gate['subprocess'], 'check_output', return_value=mount), mock.patch.object(gate['subprocess'], 'run', return_value=SimpleNamespace(returncode=mount_code)), mock.patch.object(gate['os'].path, 'samefile', return_value=same):
            try:
                gate['main']()
            except RuntimeError:
                accepted = False
        require(accepted == expected, f'Preflight accepted an invalid mount or install state: {mount}, {status}')


def verify_script_pin(setup):
    # Test checksum handling with our own bytes; an upstream installer is not a build input.
    content = b'isolated checksum fixture, not an installer\n'
    policy = dict(POLICY, installer_sha256=hashlib.sha256(content).hexdigest())
    response = io.BytesIO(content)
    response.geturl = lambda: policy['installer_url']
    fake_target = SimpleNamespace(open=lambda mode: io.BytesIO())
    with mock.patch.dict(setup, {'Path': lambda value: fake_target}), mock.patch.object(setup['urllib'].request, 'urlopen', return_value=response):
        setup['download_installer'](policy, 'memory-only')
    altered = io.BytesIO(b'altered installer')
    altered.geturl = lambda: policy['installer_url']
    with mock.patch.dict(setup, {'Path': lambda value: fake_target}), mock.patch.object(setup['urllib'].request, 'urlopen', return_value=altered):
        try:
            setup['download_installer'](policy, 'memory-only')
        except setup['SetupError']:
            pass
        else:
            raise AssertionError('Altered installer passed checksum verification.')
    insecure = io.BytesIO(content)
    insecure.geturl = lambda: 'http://mirror.example/install.sh'
    with mock.patch.dict(setup, {'Path': lambda value: fake_target}), mock.patch.object(setup['urllib'].request, 'urlopen', return_value=insecure):
        expect_rejection(lambda _: setup['download_installer'](policy, 'memory-only'), None)


def verify_mirror_selection(setup, base_plan):
    answers = ['2', 'https://mirror.example/k3s/install.sh',
               'https://mirror.example/k3s/releases/download', 'y', 'docker.io,registry.k8s.io',
               'https://docker.example', 'https://registry.example']
    with mock.patch('builtins.input', side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
        source = setup['collect_download_source'](POLICY)
    plan = dict(base_plan, download_source=source)
    require(source['kind'] == 'mirror', 'Custom source was not selected.')
    registry = json.loads(setup['registry_text'](plan))
    require(registry['mirrors']['docker.io']['endpoint'] == ['https://docker.example'], 'Container mirror was lost.')
    require('disable-default-registry-endpoint: true' in setup['config_text'](POLICY, plan), 'Configured mirror silently falls back to upstream.')
    verify_online_pipeline(setup, plan)
    downloads = []
    def fake_download(policy, destination):
        downloads.append(policy)
    patches = {'save_state': lambda *args: None, 'private_write': lambda *args: None,
               'prepare_storage': lambda *args: 'k3s', 'run': lambda *args, **kwargs: 'active',
               'download_installer': fake_download}
    with mock.patch.dict(setup, patches), mock.patch.object(setup['tempfile'], 'TemporaryDirectory', return_value=contextlib.nullcontext(str(ROOT))), contextlib.redirect_stdout(io.StringIO()):
        setup['install'](POLICY, {'plan': plan}, None)
    require(downloads[0]['installer_url'] == source['installer_url'], 'Selected script source was lost.')
    require(downloads[0]['installer_sha256'] == POLICY['installer_sha256'], 'Mirror bypassed the script checksum.')


def verify_noninteractive():
    environment = os.environ.copy()
    environment['PYTHONIOENCODING'] = 'utf-8'
    result = subprocess.run([sys.executable, str(SETUP), '--auto'], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8', env=environment)
    require(result.returncode == 1 and '交互式 SSH' in result.stderr, 'Noninteractive invocation was not rejected.')
    bash = shutil.which('bash')
    git_bash = Path(r'C:\Program Files\Git\bin\bash.exe')
    if git_bash.exists():
        bash = str(git_bash)
    if not bash:
        raise AssertionError('Bash is required to verify the login hook.')
    profile = str(OVERLAY / 'etc/profile.d/armhpc-configuration.sh').replace('\\', '/')
    subprocess.run([bash, '-n', profile], check=True)
    environment['SSH_CONNECTION'] = 'verification-only'
    result = subprocess.run([bash, '--noprofile', '--norc', '-c', '. "$1"', '--', profile], capture_output=True, env=environment)
    require(result.returncode == 0 and not result.stdout and not result.stderr, 'Noninteractive SSH was intercepted by the login hook.')


def verify_architectures(setup):
    """Exercise real host guards with simulated ARM32 and ARM64 hosts."""
    for arch, machine in [('arm64', 'aarch64'), ('armhf', 'armv7l')]:
        policy = dict(POLICY, architecture=arch, runtime_machines=ARCHITECTURES[arch]['runtime_machines'])
        policy.update(data_mountpoint='/srv/persistent', data_label='NODE_DATA')
        def fake_path(path):
            content = 'memory cpu pids' if str(path).endswith('cgroup.controllers') else 'Filename Type Size Used Priority\n'
            return SimpleNamespace(is_dir=lambda: True, exists=lambda: True, read_text=lambda: content)
        def fake_run(args, **kwargs):
            if args[0] == 'dpkg':
                return arch
            if args[3] == '/':
                return 'ext4'
            require(args[3] == '/srv/persistent', 'Host guard uses a fixed data mountpoint.')
            return '/srv/persistent ext4 NODE_DATA'
        with mock.patch.dict(setup, {'Path': fake_path, 'run': fake_run}), mock.patch.object(setup['sys'], 'platform', 'linux'), mock.patch.object(setup['platform'], 'machine', return_value=machine), mock.patch.object(setup['shutil'], 'which', return_value='/test/command'):
            setup['check_host'](policy)
            other = 'armhf' if arch == 'arm64' else 'arm64'
            expect_rejection(setup['check_host'], dict(policy, runtime_machines=ARCHITECTURES[other]['runtime_machines']))


def verify_configurable_preflight():
    gate = namespace(PREFLIGHT)
    policy = dict(POLICY, data_mountpoint='/srv/persistent', data_label='NODE_DATA',
                  kubelet_source='/srv/persistent/kubelet', node_identity_source='/srv/persistent/node-config/k3s/node')
    commands, bindings = [], []
    def fake_path(path):
        value = policy if path == '/etc/image-framework/k3s.json' else {'status': 'installed'}
        return SimpleNamespace(read_text=lambda: json.dumps(value))
    def fake_check(args, **kwargs):
        commands.append(args)
        return '/srv/persistent ext4 NODE_DATA'
    def fake_same(source, target):
        bindings.append((source, target))
        return True
    with mock.patch.dict(gate, {'Path': fake_path}), mock.patch.object(gate['subprocess'], 'check_output', side_effect=fake_check), mock.patch.object(gate['subprocess'], 'run', return_value=SimpleNamespace(returncode=0)), mock.patch.object(gate['os'].path, 'samefile', side_effect=fake_same):
        gate['main']()
    require(commands[0][3] == '/srv/persistent', 'Preflight uses a fixed data mountpoint.')
    require(bindings[0][0] == policy['kubelet_source'] and bindings[1][0] == policy['node_identity_source'], 'Preflight ignores target binding paths.')


def main():
    for path in (SETUP, PREFLIGHT):
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    setup = namespace(SETUP)
    verify_inputs(setup)
    plans = verify_wizard(setup)
    verify_online_pipeline(setup, plans[0])
    verify_online_pipeline(setup, plans[0], dict(POLICY, architecture='armhf', installer_arch='arm'))
    verify_mirror_selection(setup, plans[0])
    verify_preflight()
    verify_architectures(setup)
    verify_configurable_preflight()
    verify_script_pin(setup)
    verify_noninteractive()
    require(not POLICY['include_k3s_binary_in_image'] and not POLICY['include_airgap_archive_in_image'], 'Image policy embeds K3s payloads.')
    print('PASS: role prompts, source selection, custom mirrors, container registry policy, credential separation, version pin, installer checksum, failure handling, storage gate, noninteractive SSH.')
    print('Scope: isolated local checks including ARM32/ARM64 and configurable mounts; no downloads, installation, system changes, or hardware acceptance.')


if __name__ == '__main__':
    main()
