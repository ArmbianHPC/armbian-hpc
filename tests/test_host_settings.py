# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Hostname persistence and network transactions without touching host interfaces."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'components/common/rootfs-overlay/usr/lib/armhpc'
sys.path.insert(0, str(LIB))
import config_io
import hostname_config as hostname
import network_config as network
import firstboot


class HostSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='armhpc-host-settings-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for directory in ('etc/armhpc', 'etc/systemd/network', 'proc/sys/kernel/random', 'sys/class/net/eth0/device'):
            (self.root / directory).mkdir(parents=True)
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('boot-one')
        (self.root / 'sys/class/net/eth0/carrier').write_text('1')
        (self.root / 'etc/hostname').write_text('armbian\n')
        (self.root / 'etc/hosts').write_text('127.0.0.1 localhost\n127.0.1.1 armbian local-alias # keep\n10.1.1.2 storage\n')
        self.net = network.Network(self.root)

    def static(self):
        return network.settings('eth0', address='192.168.100.20/24', gateway='192.168.100.1', dns=['192.168.100.1,1.1.1.1'])

    def submit(self, value=None):
        record = self.net.submit(value or self.static())
        self.net.apply(record['transaction_id'])
        return record['transaction_id']

    def test_manual_system_hostname_survives_repeated_boots_and_rescue_remains_automatic(self):
        hostname.set_hostname('compute-07', self.root, runtime=False)
        for _ in range(2):
            self.assertEqual(firstboot.configure_hostname('02:11:22:33:ce:58', self.root, runtime=False), 'compute-07')
        self.assertIn('local-alias # keep', (self.root / 'etc/hosts').read_text())
        self.assertIn('10.1.1.2 storage', (self.root / 'etc/hosts').read_text())
        (self.root / 'etc/armhpc/rescue-environment').touch()
        self.assertEqual(firstboot.configure_hostname('02:11:22:33:ce:58', self.root, runtime=False), 'armhpc-rescue-ce58')
        with self.assertRaises(config_io.ConfigError):
            hostname.set_hostname('rescue-custom', self.root, runtime=False)

    def test_hostnamectl_or_manual_hostname_file_is_respected_without_a_custom_marker(self):
        (self.root / 'etc/hostname').write_text('existing.example.org\n')
        self.assertEqual(firstboot.configure_hostname('02:11:22:33:ce:58', self.root, runtime=False), 'existing.example.org')
        self.assertFalse((self.root / 'etc/armhpc/hostname.json').exists())

    def test_factory_defaults_and_invalid_hostnames_receive_a_unique_default(self):
        for value in ('', 'armbian', 'localhost', 'armhpc', 'wrong/name'):
            (self.root / 'etc/hostname').write_text(value)
            self.assertEqual(firstboot.configure_hostname('02:11:22:33:ce:58', self.root, runtime=False), 'armhpc-ce58')

    def test_boot_recovery_precedes_network_identity_and_authentication(self):
        order = []
        with mock.patch.object(firstboot, 'recover_network_settings', side_effect=lambda: order.append('recover')), \
             mock.patch.object(firstboot, 'configure_network', side_effect=lambda: order.append('identity')), \
             mock.patch.object(firstboot, 'complete_password_access', side_effect=lambda: order.append('authentication')), \
             mock.patch.object(firstboot.Path, 'is_file', return_value=True):
            firstboot.main()
        self.assertEqual(order, ['recover', 'identity', 'authentication'])

    def test_hostname_initialization_error_keeps_authentication_available(self):
        from contextlib import redirect_stdout
        import io
        with mock.patch.object(firstboot, 'recover_network_settings'), \
             mock.patch.object(firstboot, 'configure_network', side_effect=config_io.ConfigError('configuration_path', 'broken path')), \
             mock.patch.object(firstboot, 'complete_password_access') as authenticate, \
             mock.patch.object(firstboot.Path, 'is_file', return_value=True), redirect_stdout(io.StringIO()):
            firstboot.main()
        authenticate.assert_called_once()

    def test_invalid_hostnames_do_not_modify_files(self):
        original = (self.root / 'etc/hosts').read_bytes()
        for name in ('a' * 65, 'armbian', 'a..b', '-node', 'node-', 'node/name', 'node\nother', 'node;reboot', '机器'):
            with self.subTest(name=name), self.assertRaises(config_io.ConfigError):
                hostname.set_hostname(name, self.root, runtime=False)
        self.assertEqual((self.root / 'etc/hosts').read_bytes(), original)

    def test_failed_hostname_write_restores_previous_files(self):
        original = {name: (self.root / 'etc' / name).read_bytes() for name in ('hostname', 'hosts')}
        real = hostname.atomic_write
        calls = 0
        def fail_once(*arguments):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('interrupted hostname write')
            return real(*arguments)
        with mock.patch.object(hostname, 'atomic_write', side_effect=fail_once), self.assertRaises(OSError):
            hostname.set_hostname('new-node', self.root, runtime=False)
        self.assertEqual({name: (self.root / 'etc' / name).read_bytes() for name in original}, original)

    def test_symlinked_hostname_or_network_directory_is_refused(self):
        target = self.root / 'outside-hostname'
        target.write_text('keep')
        (self.root / 'etc/hostname').unlink()
        (self.root / 'etc/hostname').symlink_to(target)
        with self.assertRaises(config_io.ConfigError):
            hostname.set_hostname('new-node', self.root, runtime=False)
        self.assertEqual(target.read_text(), 'keep')
        folder = self.root / 'etc/systemd/network'
        folder.rmdir()
        folder.symlink_to(self.root / 'etc/armhpc', target_is_directory=True)
        with self.assertRaises(config_io.ConfigError):
            self.net.submit(self.static())

    def test_network_plan_is_read_only_and_dns_hostname_ipv6_rendering_is_explicit(self):
        plan = self.net.plan(self.static())
        self.assertIn('Address=192.168.100.20/24', plan['content'])
        self.assertIn('Gateway=192.168.100.1', plan['content'])
        self.assertEqual(plan['content'].count('UseDNS=no'), 3)
        self.assertEqual(plan['content'].count('UseHostname=no'), 2)
        self.assertIn('IPv6AcceptRA=yes', plan['content'])
        self.assertIn('DHCP=ipv6', plan['content'])
        self.assertFalse(self.net.state.exists())
        self.assertFalse(self.net.config_path('eth0').exists())
        self.assertIn('DHCP=yes', network.render(network.settings('eth0', dhcp=True)))

    def test_invalid_network_values_are_rejected_before_state_is_written(self):
        for arguments in ({'interface': '../eth0', 'dhcp': True}, {'interface': 'eth0'},
                          {'interface': 'eth0', 'address': '192.168.1.20'},
                          {'interface': 'eth0', 'address': '192.168.1.0/24'},
                          {'interface': 'eth0', 'address': '192.168.1.255/24'},
                          {'interface': 'eth0', 'address': '127.0.0.1/8'},
                          {'interface': 'eth0', 'address': '192.168.1.20/24', 'gateway': '192.168.2.1'},
                          {'interface': 'eth0', 'dhcp': True, 'dns': ['1.1.1.1\nExec=x']},
                          {'interface': 'eth0', 'dhcp': True, 'gateway': '192.168.1.1'}):
            with self.subTest(arguments=arguments), self.assertRaises((ValueError, config_io.ConfigError)):
                network.settings(**arguments)
        for timeout in (29, 601):
            with self.assertRaises(config_io.ConfigError):
                self.net.submit(self.static(), timeout)
        self.assertFalse(self.net.state.exists())

    def test_confirmed_network_survives_reboot_and_other_interface_files_are_untouched(self):
        unrelated = self.root / 'etc/systemd/network/10-other.network'
        unrelated.write_text('keep-this')
        identifier = self.submit()
        self.net.confirm(identifier)
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('boot-two')
        self.net.recover_on_boot()
        self.assertEqual(self.net.record()['stage'], 'confirmed')
        self.assertIn('Address=192.168.100.20/24', self.net.config_path('eth0').read_text())
        self.assertEqual(unrelated.read_text(), 'keep-this')

    def test_unconfirmed_change_times_out_to_previous_config_and_mode(self):
        path = self.net.config_path('eth0')
        previous = network.render(network.settings('eth0', dhcp=True))
        path.write_text(previous)
        path.chmod(0o640)
        identifier = self.submit()
        with mock.patch.object(network, 'monotonic', return_value=self.net.record()['deadline'] + 1):
            self.assertFalse(self.net.expire(identifier))
        self.assertEqual(path.read_text(), previous)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.net.record()['stage'], 'rolled-back')

    def test_unconfirmed_change_is_removed_before_network_initialization_on_reboot(self):
        self.submit()
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('boot-two')
        with mock.patch.object(self.net, 'reload') as reload:
            self.net.recover_on_boot()
            reload.assert_not_called()
        self.assertFalse(self.net.config_path('eth0').exists())
        self.assertEqual(self.net.record()['stage'], 'rolled-back')

    def test_queued_but_unapplied_change_can_be_rolled_back_after_service_interruption(self):
        self.net.submit(self.static())
        self.net.recover_on_boot()
        self.assertEqual(self.net.record()['stage'], 'rolled-back')
        self.assertFalse(self.net.config_path('eth0').exists())

    def test_failed_live_apply_restores_prior_file_and_keeps_error(self):
        identifier = self.net.submit(self.static())['transaction_id']
        with mock.patch.object(self.net, 'reload', side_effect=[OSError('reload failed'), None]):
            result = self.net.apply(identifier)
        self.assertEqual(result['stage'], 'rolled-back')
        self.assertIn('reload failed', result['error'])
        self.assertFalse(self.net.config_path('eth0').exists())

    def test_preapply_failure_finishes_without_overwriting_external_changes(self):
        identifier = self.net.submit(self.static())['transaction_id']
        path = self.net.config_path('eth0')
        changed = network.render(network.settings('eth0', dhcp=True))
        path.write_text(changed)
        result = self.net.apply(identifier)
        self.assertEqual(result['stage'], 'failed')
        self.assertEqual(path.read_text(), changed)
        self.assertFalse(self.net.expire(identifier))

    def test_old_watcher_does_not_touch_a_new_network_transaction(self):
        old = self.submit()
        self.net.confirm(old)
        new = self.net.submit(network.settings('eth0', dhcp=True))['transaction_id']
        self.assertFalse(self.net.expire(old))
        self.assertEqual(self.net.record()['transaction_id'], new)
        self.assertEqual(self.net.record()['stage'], 'queued')

    def test_corrupt_transaction_is_diagnosed_without_modifying_config(self):
        self.submit()
        content = self.net.config_path('eth0').read_bytes()
        value = self.net.record()
        value['deadline'] = float('nan')
        self.net.state.write_text(json.dumps(value))
        with self.assertRaises(config_io.ConfigError):
            self.net.recover_on_boot()
        self.assertEqual(self.net.config_path('eth0').read_bytes(), content)

    def test_conflicting_change_and_stale_confirmation_are_refused(self):
        identifier = self.submit()
        with self.assertRaises(config_io.ConfigError):
            self.net.submit(network.settings('eth0', dhcp=True))
        with self.assertRaises(config_io.ConfigError):
            self.net.confirm(str(__import__('uuid').uuid4()))
        (self.root / 'sys/class/net/eth0/carrier').write_text('0')
        with self.assertRaises(config_io.ConfigError):
            self.net.confirm(identifier)
        self.assertEqual(self.net.record()['stage'], 'pending')

    def test_manual_edit_is_not_overwritten_by_timeout_rollback(self):
        identifier = self.submit()
        self.net.config_path('eth0').write_text('manually-edited')
        with self.assertRaises(config_io.ConfigError):
            self.net.rollback(identifier)
        self.assertEqual(self.net.record()['stage'], 'rollback-failed')
        self.assertEqual(self.net.config_path('eth0').read_text(), 'manually-edited')

    def test_pending_and_confirmed_transaction_files_are_private_and_status_excludes_backups(self):
        identifier = self.submit()
        self.assertEqual(self.net.state.stat().st_mode & 0o777, 0o600)
        status = self.net.status()
        self.assertNotIn('backup', status['transaction'])
        self.assertEqual(status['transaction']['transaction_id'], identifier)

    def test_two_physical_interfaces_require_explicit_selection(self):
        (self.root / 'sys/class/net/eth1/device').mkdir(parents=True)
        with self.assertRaises(config_io.ConfigError):
            self.net.choose_interface()
        self.assertEqual(self.net.choose_interface('eth1'), 'eth1')

    def test_guide_collects_dhcp_or_static_settings(self):
        with mock.patch.object(sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', side_effect=['', '']):
            self.assertEqual(network.wizard(self.net)['method'], 'dhcp')
        with mock.patch.object(sys.stdin, 'isatty', return_value=True), mock.patch('builtins.input', side_effect=['static', '192.168.1.9/24', '192.168.1.1', '1.1.1.1,8.8.8.8']):
            value = network.wizard(self.net)
        self.assertEqual(value['address'], '192.168.1.9/24')
        self.assertEqual(value['dns'], ['1.1.1.1', '8.8.8.8'])

    def test_configuration_and_ota_share_the_same_writer_lock(self):
        with config_io.configuration_lock(self.root), self.assertRaises(config_io.ConfigError):
            hostname.set_hostname('blocked-node', self.root, runtime=False)
        self.assertEqual((self.root / 'etc/hostname').read_text(), 'armbian\n')


if __name__ == '__main__':
    unittest.main()
