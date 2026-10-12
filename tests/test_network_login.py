# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Exercise bounded SSH-login decisions over a real Linux pseudoterminal."""
from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

import test_host_settings as fixtures
network = fixtures.network
config_io = fixtures.config_io

LIB = Path(__file__).resolve().parents[1] / 'components/common/rootfs-overlay/usr/lib/armhpc'
spec = importlib.util.spec_from_file_location('host_configuration_login', LIB / 'configuration.py')
configuration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configuration)


@unittest.skipUnless(sys.platform == 'linux', 'Requires a Linux PTY')
class NetworkLoginTests(unittest.TestCase):
    def setUp(self):
        fixtures.HostSettingsTests.setUp(self)
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        self.master = master
        self.input = os.fdopen(os.dup(slave), 'r', encoding='utf-8')
        self.addCleanup(self.input.close)
        self.output = io.StringIO()

    static = fixtures.HostSettingsTests.static
    submit = fixtures.HostSettingsTests.submit

    def prompt(self, answer=None):
        if answer is not None:
            os.write(self.master, answer.encode())
        return network.login_prompt(self.net, self.input, self.output)

    def test_no_pending_change_and_rescue_login_do_not_prompt(self):
        self.assertEqual(self.prompt(), 0)
        self.submit()
        (self.root / 'etc/armhpc/rescue-environment').touch()
        self.assertEqual(self.prompt(), 0)
        self.assertEqual(self.output.getvalue(), '')
        self.assertEqual(self.net.record()['stage'], 'pending')

    def test_new_login_only_confirms_after_explicit_yes(self):
        identifier = self.submit()
        self.assertEqual(self.prompt('y\n'), 10)
        self.assertEqual(self.net.record()['stage'], 'confirmed')
        self.assertEqual(self.net.record()['transaction_id'], identifier)
        self.assertIn('新网络配置已确认并保存', self.output.getvalue())
        self.assertIn('剩余约', self.output.getvalue())

    def test_enter_enters_shell_and_does_not_extend_deadline(self):
        self.submit()
        before = self.net.record()['deadline']
        self.assertEqual(self.prompt('\n'), 10)
        self.assertEqual(self.net.record()['stage'], 'pending')
        self.assertEqual(self.net.record()['deadline'], before)
        self.assertIn('手动确认：', self.output.getvalue())

    def test_explicit_rollback_restores_prior_profile(self):
        self.submit()
        self.assertEqual(self.prompt('r\n'), 10)
        self.assertEqual(self.net.record()['stage'], 'rolled-back')
        self.assertFalse(self.net.config_path('eth0').exists())

    def test_no_input_times_out_at_original_deadline(self):
        identifier = self.submit()
        record = self.net.record()
        record['deadline'] = network.monotonic() + 0.15
        config_io.save_json(self.net.state, record)
        started = time.monotonic()
        self.assertEqual(self.prompt(), 10)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(self.net.record()['stage'], 'pending')
        self.assertFalse(self.net.expire(identifier))
        self.assertEqual(self.net.record()['stage'], 'rolled-back')
        self.assertIn('超时', self.output.getvalue())

    def test_invalid_answer_does_not_restart_countdown(self):
        self.submit()
        before = self.net.record()['deadline']
        self.assertEqual(self.prompt('?\ny\n'), 10)
        self.assertEqual(self.net.record()['stage'], 'confirmed')
        self.assertEqual(self.net.record()['deadline'], before)
        self.assertIn('请输入 y、r', self.output.getvalue())

    def test_late_login_cannot_confirm_an_expired_transaction(self):
        self.submit()
        record = self.net.record()
        record['deadline'] = network.monotonic() - 1
        config_io.save_json(self.net.state, record)
        with mock.patch.object(self.net, 'confirm') as confirm:
            self.assertEqual(self.prompt('y\n'), 10)
            confirm.assert_not_called()
        self.assertEqual(self.net.record()['stage'], 'pending')

    def test_replaced_transaction_is_not_confirmed_by_old_prompt(self):
        old = self.submit()
        def replace(*args):
            self.net.confirm(old)
            self.net.submit(network.settings('eth0', dhcp=True))
            return [self.input], [], []
        with mock.patch.object(network.select, 'select', side_effect=replace):
            self.assertEqual(self.prompt('y\n'), 10)
        self.assertNotEqual(self.net.record()['transaction_id'], old)
        self.assertEqual(self.net.record()['stage'], 'queued')
        self.assertIn('未完成', self.output.getvalue())

    def test_another_session_confirmation_ends_wait_without_false_timeout(self):
        identifier = self.submit()
        def elsewhere(*args):
            self.net.confirm(identifier)
            return [], [], []
        with mock.patch.object(network.select, 'select', side_effect=elsewhere):
            self.assertEqual(self.prompt(), 10)
        self.assertEqual(self.net.record()['stage'], 'confirmed')
        self.assertIn('已经确认', self.output.getvalue())
        self.assertNotIn('已超时', self.output.getvalue())

    def test_noninteractive_login_entry_never_reads_transactions(self):
        with mock.patch.object(network.sys.stdin, 'isatty', return_value=False), mock.patch.object(network, 'Network') as constructor:
            self.assertEqual(network.main(['_login']), 0)
            constructor.assert_not_called()

    def test_pending_notice_runs_before_workload_menu_and_keeps_shell_available(self):
        with mock.patch.object(sys, 'argv', ['armhpc configuration', '--auto']), \
             mock.patch.object(configuration.sys.stdin, 'isatty', return_value=True), \
             mock.patch.object(configuration.sys.stdout, 'isatty', return_value=True), \
             mock.patch.object(configuration, 'network_login_confirmation', return_value=True) as notice, \
             mock.patch.object(configuration.Path, 'read_text') as read:
            configuration.main()
            notice.assert_called_once()
            read.assert_not_called()

    def test_noninteractive_application_configuration_never_prompts(self):
        with mock.patch.object(sys, 'argv', ['armhpc configuration', '--auto']), \
             mock.patch.object(configuration.sys.stdin, 'isatty', return_value=False), \
             mock.patch.object(configuration, 'network_login_confirmation') as notice:
            configuration.main()
            notice.assert_not_called()

    def test_privileged_administrator_uses_existing_sudo_without_password_prompt(self):
        with mock.patch.object(configuration.os, 'geteuid', return_value=1000), \
             mock.patch.object(configuration.Path, 'is_file', return_value=False), \
             mock.patch.object(configuration.subprocess, 'run', return_value=mock.Mock(returncode=10)) as execute:
            self.assertTrue(configuration.network_login_confirmation())
        self.assertEqual(execute.call_args.args[0][:3], ['sudo', '-n', '--'])
        self.assertEqual(execute.call_args.args[0][-1], '_login')


if __name__ == '__main__':
    unittest.main()
