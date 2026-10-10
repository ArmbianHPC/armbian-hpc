# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Offline administrator provisioning checks with all host commands mocked."""

import base64
import contextlib
import io
import json
from pathlib import Path
import runpy
import struct
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / 'components/common/rootfs-overlay/usr/lib/armhpc/admin.py'


class AdminTests(unittest.TestCase):
    def setUp(self):
        test_root = ROOT / 'build/tests'
        test_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='admin-', dir=test_root)
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.cleanup)
        self.ns = runpy.run_path(str(HELPER), run_name='admin_verify')['main'].__globals__
        self.root.joinpath('etc/ssh').mkdir(parents=True)
        self.root.joinpath('etc/passwd').write_text('root:x:0:0:root:/root:/bin/bash\n')
        self.root.joinpath('etc/ssh/sshd_config').write_text('PasswordAuthentication yes\n')
        self.root.joinpath('root').mkdir()
        self.root.joinpath('root/.not_logged_in_yet').write_text('fixture')
        self.key_file = self.root / 'administrator.pub'
        kind = b'ssh-ed25519'
        blob = struct.pack('>I', len(kind)) + kind + struct.pack('>I', 32) + b'a' * 32
        self.key_file.write_text('ssh-ed25519 ' + base64.b64encode(blob).decode() + ' test-local-comment\n')
        self.commands = []

    def cleanup(self):
        self.assertTrue(self.root.is_relative_to((ROOT / 'build/tests').resolve()))
        self.temporary.cleanup()

    def fake_target(self, root, *args):
        self.assertEqual(root, self.root)
        self.commands.append(args)
        if args[0] == '/usr/sbin/useradd':
            self.root.joinpath('etc/passwd').write_text('root:x:0:0:root:/root:/bin/bash\nhpc:x:1000:1000::/home/hpc:/bin/bash\n')
            self.root.joinpath('home/hpc').mkdir(parents=True)
        if args[0] == '/usr/sbin/sshd' and '-T' in args:
            return ('permitrootlogin no\npasswordauthentication no\nkbdinteractiveauthentication no\n'
                    'pubkeyauthentication yes\nauthenticationmethods publickey\nusepam yes\n'
                    'authorizedkeysfile .ssh/authorized_keys\nallowusers hpc\n')
        return ''

    def invoke(self, fake_target=None):
        patches = {'check_offline_root': lambda root: None, 'in_target': fake_target or self.fake_target}
        with mock.patch.dict(self.ns, patches), mock.patch.object(self.ns['os'], 'chown', create=True), mock.patch.object(self.ns['os'], 'fchmod', create=True), contextlib.redirect_stdout(io.StringIO()):
            return self.ns['provision'](self.root, 'hpc', self.key_file)

    def test_creates_key_admin_checks_sshd_then_marks_complete(self):
        receipt = self.invoke()
        self.assertEqual(receipt['username'], 'hpc')
        key = self.root.joinpath('home/hpc/.ssh/authorized_keys').read_text()
        self.assertNotIn('test-local-comment', key)
        self.assertIn('NOPASSWD: ALL', self.root.joinpath('etc/sudoers.d/90-image-framework-admin').read_text())
        self.assertTrue(self.root.joinpath('etc/image-framework/admin-provisioned.json').exists())
        self.assertFalse(self.root.joinpath('root/.not_logged_in_yet').exists())
        self.assertFalse(self.root.joinpath('etc/image-framework/admin-provisioning.json').exists())
        self.assertEqual(self.commands[-1], ('/usr/sbin/sshd', '-t'))

    def test_failed_effective_config_keeps_ssh_closed_and_retry_works(self):
        def fail_target(root, *args):
            result = self.fake_target(root, *args)
            return result.replace('passwordauthentication no', 'passwordauthentication yes')
        with self.assertRaisesRegex(self.ns['ProvisionError'], 'SSH 有效配置'):
            self.invoke(fail_target)
        self.assertFalse(self.root.joinpath('etc/image-framework/admin-provisioned.json').exists())
        self.assertTrue(self.root.joinpath('etc/image-framework/admin-provisioning.json').exists())
        self.invoke()
        self.assertTrue(self.root.joinpath('etc/image-framework/admin-provisioned.json').exists())
        self.assertEqual(sum(cmd[0] == '/usr/sbin/useradd' for cmd in self.commands), 1)

    def test_plain_existing_user_is_not_taken_over(self):
        self.root.joinpath('etc/passwd').write_text('hpc:x:1000:1000::/home/hpc:/bin/bash\n')
        with self.assertRaisesRegex(self.ns['ProvisionError'], '现有账户'):
            self.invoke()
        self.assertEqual(self.commands, [])

    def test_private_key_options_and_mismatched_key_type_are_rejected(self):
        valid = self.key_file.read_text()
        for content in ('-----BEGIN OPENSSH PRIVATE KEY-----\nfixture',
                        'command="id" ' + valid, valid + valid, valid.replace('ssh-ed25519 ', 'ssh-rsa ', 1)):
            with self.subTest(content=content), self.assertRaises(self.ns['ProvisionError']):
                self.ns['parse_public_key'](content)

    def test_path_traversal_and_symlink_cannot_escape_target(self):
        for relative in ('../etc/passwd', '/etc/passwd', 'etc\\passwd'):
            with self.subTest(relative=relative), self.assertRaises(self.ns['ProvisionError']):
                self.ns['target_path'](self.root, relative)
        # Windows may not grant symlink creation; traversal checks above still run.
        try:
            (self.root / 'outside').symlink_to(self.root.parent, target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(self.ns['ProvisionError']):
            self.ns['target_path'](self.root, 'outside/passwd')

    def test_live_root_or_unmounted_target_is_rejected(self):
        with mock.patch.object(self.ns['sys'], 'platform', 'linux'), mock.patch.object(self.ns['os'], 'geteuid', return_value=0, create=True):
            with self.assertRaises(self.ns['ProvisionError']):
                self.ns['check_offline_root'](Path('/'))
            with mock.patch.object(self.ns['subprocess'], 'run', return_value=mock.Mock(returncode=1, stdout='')):
                with self.assertRaisesRegex(self.ns['ProvisionError'], '独立挂载'):
                    self.ns['check_offline_root'](self.root)

    def test_a_bind_of_the_live_root_is_rejected_before_target_commands(self):
        with mock.patch.object(self.ns['sys'], 'platform', 'linux'), mock.patch.object(self.ns['os'], 'geteuid', return_value=0, create=True), mock.patch.object(self.ns['os'].path, 'samefile', return_value=True), mock.patch.object(self.ns['subprocess'], 'run') as command:
            with self.assertRaisesRegex(self.ns['ProvisionError'], '绑定挂载'):
                self.ns['check_offline_root'](self.root)
            command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
