# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'components/k3s/rootfs-overlay/usr/lib/armhpc/kube_manage.py'
spec = importlib.util.spec_from_file_location('kube_manage', MODULE)
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


class Runner:
    def __init__(self, root):
        self.root = root
        self.active = True
        self.calls = []
        self.fail_uninstall = False
        self.extra_mount = ''

    def __call__(self, *args, **kwargs):
        args = tuple(map(str, args))
        self.calls.append((args, kwargs))
        out, rc = '', 0
        if args[0] == 'findmnt':
            out = '/data ext4 OECT_DATA\n' if '-M' in args else '/\n/data\n' + self.extra_mount
        elif args[0] == 'mountpoint':
            rc = 1
        elif args[:2] == ('systemctl', 'show'):
            out = 'LoadState=loaded\nActiveState=' + ('active' if self.active else 'inactive') + '\nMainPID=' + ('55' if self.active else '0') + '\n'
        elif args[:2] == ('systemctl', 'stop'):
            self.active = False
        elif args[0] == 'sh' and args[1].endswith('uninstall.sh'):
            if self.fail_uninstall:
                self.fail_uninstall = False
                rc = 1
            else:
                (self.root / 'usr/local/bin/k3s').unlink(missing_ok=True)
        return subprocess.CompletedProcess(args, rc, out, '')


class KubeManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy = {'data_mountpoint': '/data', 'data_filesystem': 'ext4', 'data_label': 'OECT_DATA',
                       'data_dir': '/data/k3s', 'configuration_dir': '/data/node-config/k3s',
                       'state_file': '/data/node-config/k3s/setup-state.json',
                       'kubelet_source': '/data/kubelet', 'kubelet_target': '/var/lib/kubelet',
                       'node_identity_source': '/data/node-config/k3s/node', 'node_identity_target': '/etc/rancher/node',
                       'local_pv_dir': '/data/pv', 'backups_dir': '/data/backups',
                       'completion_marker': '/var/lib/image-framework/k3s/setup-complete',
                       'default_version': 'v1.36.5+k3s1'}
        self.write('/etc/image-framework/k3s.json', json.dumps(self.policy))
        self.write('/data/node-config/k3s/setup-state.json', json.dumps({'status': 'failed',
            'plan': {'role': 'server-join', 'version': 'v1.36.5+k3s1'}}))
        self.write('/data/k3s/server/db/value', 'old-cluster')
        self.write('/data/node-config/k3s/node/password', 'old-node-password')
        self.write('/data/kubelet/pod-state', 'old-pod')
        self.write('/data/pv/claim/data', 'old-pv')
        self.write('/usr/local/bin/k3s', 'binary')
        self.write('/usr/local/bin/k3s-killall.sh', '#!/bin/sh\n')
        self.write('/usr/local/bin/k3s-uninstall.sh', '#!/bin/sh\n')
        self.write('/etc/systemd/system/k3s.service', '[Service]\n')
        self.write('/etc/systemd/system/k3s.service.d/storage.conf', 'old-storage')
        self.write('/etc/rancher/node/password', 'old-node-password')
        self.write('/etc/rancher/k3s/k3s.yaml', 'private-config')
        self.write('/etc/ssh/ssh_host_ed25519_key', 'ssh-key')
        self.write('/etc/hostname', 'node-one\n')
        self.write('/boot/Image', 'kernel')
        self.write('/data/user-file', 'keep-this')
        self.write('/etc/fstab', 'LABEL=OECT_DATA /data ext4 defaults 0 2\n'
            '/data/kubelet /var/lib/kubelet none bind 0 0\n'
            '/data/node-config/k3s/node /etc/rancher/node none bind 0 0\n'
            'LABEL=OTA_SHARED /var/lib/armhpc/ota ext4 defaults 0 2\n')
        self.runner = Runner(self.root)
        self.manager = manage.Manager(self.root, self.runner)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, value):
        p = self.root / path.lstrip('/')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)
        p.chmod(0o700 if p.name.endswith('.sh') else 0o600)

    def test_uninstall_archives_state_and_preserves_unrelated_data_identity_boot(self):
        self.write('/data/node-config/armhpc/state.json',json.dumps({'schema_version':1,'data':{},
            'services':{'k3s.service':{'enabled':True},'other.service':{'enabled':True}}}))
        self.write('/var/lib/armhpc/auth/state.json','keep-authentication')
        result = self.manager.execute(self.manager.plan('uninstall', 'v1.37.1+k3s1'))
        backup = Path(result['backup'])
        self.assertEqual('completed', result['stage'])
        self.assertEqual('old-cluster', (backup / 'data/data/k3s/server/db/value').read_text())
        self.assertEqual('old-pv', (backup / 'data/data/pv/claim/data').read_text())
        self.assertEqual([], list((self.root / 'data/k3s').iterdir()))
        self.assertFalse((self.root / 'data/node-config/k3s/setup-state.json').exists())
        self.assertEqual('v1.37.1+k3s1', json.loads((self.root / 'etc/image-framework/k3s.json').read_text())['default_version'])
        for path, value in (('etc/ssh/ssh_host_ed25519_key', 'ssh-key'), ('etc/hostname', 'node-one\n'),
                            ('boot/Image', 'kernel'), ('data/user-file', 'keep-this')):
            self.assertEqual(value, (self.root / path).read_text())
        self.assertNotIn('none bind', (self.root / 'etc/fstab').read_text())
        self.assertIn('OTA_SHARED', (self.root / 'etc/fstab').read_text())
        native = [kwargs['env']['K3S_DATA_DIR'] for args, kwargs in self.runner.calls if args[0] == 'sh']
        self.assertTrue(native)
        self.assertEqual({str(self.root / 'data/k3s')}, set(native))
        self.assertEqual(0o700, backup.stat().st_mode & 0o777)
        catalog=json.loads((self.root/'data/node-config/armhpc/state.json').read_text())
        self.assertEqual(set(catalog['services']),{'other.service'})
        self.assertTrue((backup/'node-recovery-state.json').is_file())
        self.assertEqual((self.root/'var/lib/armhpc/auth/state.json').read_text(),'keep-authentication')

    def test_clear_refuses_running_or_completed_installations(self):
        with self.assertRaisesRegex(manage.ManageError, 'unfinished'):
            self.manager.plan('clear')
        self.runner.active = False
        self.write('/data/node-config/k3s/setup-state.json', json.dumps({'status': 'installed'}))
        with self.assertRaises(manage.ManageError):
            self.manager.plan('clear')
        self.assertTrue((self.root / 'data/k3s/server/db/value').exists())

    def test_clear_archives_a_failed_partial_installation(self):
        self.runner.active = False
        result = self.manager.execute(self.manager.plan('clear', 'v1.37.1+k3s1'))
        self.assertEqual('completed', result['stage'])
        self.assertTrue((Path(result['backup']) / 'data/data/node-config/k3s/setup-state.json').exists())
        self.assertFalse((self.root / 'usr/local/bin/k3s').exists())

    def test_interrupted_cleanup_continues_the_original_backup(self):
        self.runner.active = False
        self.runner.fail_uninstall = True
        plan = self.manager.plan('clear', 'v1.37.1+k3s1')
        with self.assertRaises(manage.ManageError):
            self.manager.execute(plan)
        original = json.loads(self.manager.task_file.read_text())
        result = self.manager.execute(self.manager.plan('clear'))
        self.assertEqual(original['backup'], result['backup'])
        self.assertEqual('completed', result['stage'])
        self.assertEqual('v1.37.1+k3s1', result['next_default_version'])

    def test_policy_cannot_target_the_data_partition_root(self):
        policy = dict(self.policy, data_dir='/data')
        self.write('/etc/image-framework/k3s.json', json.dumps(policy))
        with self.assertRaises(manage.ManageError):
            manage.Manager(self.root, self.runner)
        self.assertEqual('keep-this', (self.root / 'data/user-file').read_text())

    def test_symbolic_link_data_target_is_rejected(self):
        original = self.root / 'data/k3s'
        original.rename(self.root / 'data/old-k3s')
        original.symlink_to(self.root / 'data/old-k3s')
        with self.assertRaises(manage.ManageError):
            manage.Manager(self.root, self.runner)

    def test_nested_mount_is_not_removed_by_archiving(self):
        self.runner.active = False
        self.runner.extra_mount = '/data/k3s/external\n'
        with self.assertRaisesRegex(manage.ManageError, 'mounted filesystem'):
            self.manager.execute(self.manager.plan('uninstall'))
        self.assertTrue((self.root / 'data/k3s/server/db/value').exists())

    def test_management_record_survives_system_root_replacement(self):
        self.assertEqual(self.root / 'data/backups/k3s-management.json', self.manager.task_file)
        self.runner.active = False
        result = self.manager.execute(self.manager.plan('clear'))
        self.assertEqual('completed', result['stage'])
        self.assertIsNone(self.manager.plan('clear')['previous_status'])


if __name__ == '__main__':
    unittest.main()
