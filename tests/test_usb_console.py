# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LIBRARY = ROOT / 'components/common/rootfs-overlay/usr/lib/armhpc'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


usb = load('usb_console_test', LIBRARY / 'usb_console.py')
cli = load('usb_cli_test', LIBRARY / 'cli.py')
commands = load('usb_layout_test', LIBRARY / 'command_layout.py')
kube = load('usb_kube_mount_test', ROOT / 'components/k3s/rootfs-overlay/usr/lib/armhpc/kube_setup.py')


class UsbConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gadgets = self.root / 'gadgets'
        self.udcs = self.root / 'udcs'
        self.gadgets.mkdir()
        self.udcs.mkdir()
        (self.udcs / 'fcc00000.usb').mkdir()
        self.config = {'enabled': True, 'controller_address': 'fcc00000',
                       'vendor_id': '0x0525', 'product_id': '0xa4a7'}

    def test_configures_only_the_board_controller_and_an_acm_function(self):
        (self.udcs / 'fd000000.usb').mkdir()
        self.assertEqual('fcc00000.usb', usb.configure(self.config, 'test-serial', self.gadgets, self.udcs))
        gadget = self.gadgets / usb.NAME
        self.assertEqual('fcc00000.usb', (gadget / 'UDC').read_text().strip())
        self.assertTrue((gadget / 'configs/c.1/acm.usb0').is_symlink())
        self.assertEqual('test-serial', (gadget / 'strings/0x409/serialnumber').read_text().strip())
        self.assertEqual('0x02', (gadget / 'bDeviceClass').read_text().strip())
        self.assertEqual('0x02', (gadget / 'bDeviceSubClass').read_text().strip())
        self.assertEqual('0x00', (gadget / 'bDeviceProtocol').read_text().strip())

    def test_configure_is_idempotent_and_does_not_unbind_a_connected_console(self):
        usb.configure(self.config, 'test-serial', self.gadgets, self.udcs)
        path = self.gadgets / usb.NAME / 'UDC'
        before = path.stat().st_mtime_ns
        usb.configure(self.config, 'test-serial', self.gadgets, self.udcs)
        self.assertEqual(before, path.stat().st_mtime_ns)

    def test_another_gadget_is_not_unbound_to_acquire_the_controller(self):
        other = self.gadgets / 'other-gadget'
        other.mkdir()
        (other / 'UDC').write_text('fcc00000.usb\n')
        with self.assertRaises(ValueError):
            usb.configure(self.config, 'test-serial', self.gadgets, self.udcs)
        self.assertEqual('fcc00000.usb', (other / 'UDC').read_text().strip())

    def test_missing_or_ambiguous_board_controller_is_refused(self):
        (self.udcs / 'fcc00000.usb').rmdir()
        with self.assertRaises(ValueError):
            usb.configure(self.config, 'test-serial', self.gadgets, self.udcs)
        (self.udcs / 'fcc00000.usb').mkdir()
        (self.udcs / 'fcc00000.other').mkdir()
        with self.assertRaises(ValueError):
            usb.configure(self.config, 'test-serial', self.gadgets, self.udcs)

    def test_disabled_adapter_and_invalid_descriptor_are_refused(self):
        for config in ({**self.config, 'enabled': False}, {**self.config, 'vendor_id': '../escape'}):
            with self.assertRaises(ValueError):
                usb.configure(config, 'test-serial', self.gadgets, self.udcs)

    def test_gadget_path_symlink_is_refused(self):
        destination = self.root / 'outside'
        destination.mkdir()
        (self.gadgets / usb.NAME).symlink_to(destination, target_is_directory=True)
        with self.assertRaises(ValueError):
            usb.configure(self.config, 'test-serial', self.gadgets, self.udcs)
        self.assertEqual([], list(destination.iterdir()))

    def test_help_does_not_load_modules_or_write_configfs(self):
        with patch.object(usb.subprocess, 'run') as run, redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                usb.main(['--help'])
            self.assertEqual(0, error.exception.code)
            run.assert_not_called()

    def test_boot_logs_use_the_valid_kernel_journal_option(self):
        with patch.object(usb.subprocess, 'run', return_value=type('Result', (), {'returncode': 0})()) as run:
            self.assertEqual(0, usb.main(['logs', '--previous']))
        args = run.call_args[0][0]
        self.assertIn('-k', args)
        self.assertIn('-1', args)
        self.assertNotIn('--kernel', args)

    def test_public_groups_expose_the_same_module(self):
        self.assertEqual(cli.NORMAL['usb-console'][0], cli.RESCUE['usb-console'][0])

    def test_console_service_enabling_and_login_do_not_use_autologin(self):
        root = self.root / 'offline'
        paths = ('etc/armhpc', 'usr/lib/armhpc', 'etc/systemd/system')
        for path in paths:
            (root / path).mkdir(parents=True, exist_ok=True)
        (root / 'etc/armhpc/usb-console.json').write_text(json.dumps(self.config))
        for path in ('usr/lib/armhpc/usb_console.py', 'etc/systemd/system/armhpc-usb-console.service'):
            (root / path).write_text('fixture')
        enabled = commands.enable_usb_console(root)
        self.assertEqual(2, len(enabled))
        self.assertEqual(enabled, commands.enable_usb_console(root))
        unit = ROOT / 'components/common/rootfs-overlay/etc/systemd/system/serial-getty@ttyGS0.service.d/10-armhpc-usb-console.conf'
        self.assertNotIn('--autologin', unit.read_text())
        self.assertIn('Requires=armhpc-usb-console.service', unit.read_text())

    def test_k3s_repair_accepts_the_verified_ota_data_uuid(self):
        policy = {'data_label': 'OECT_DATA', 'data_filesystem': 'ext4'}
        self.assertTrue(kube.valid_data_entry(['LABEL=OECT_DATA', '/data', 'ext4', 'defaults'], policy, 'known-uuid'))
        self.assertTrue(kube.valid_data_entry(['UUID=known-uuid', '/data', 'ext4', 'defaults'], policy, 'known-uuid'))
        self.assertFalse(kube.valid_data_entry(['UUID=another-disk', '/data', 'ext4', 'defaults'], policy, 'known-uuid'))

    def test_status_requires_host_configuration_not_just_a_bound_tty(self):
        usb.configure(self.config, 'test-serial', self.gadgets, self.udcs)
        config = self.root / 'config.json'
        config.write_text(json.dumps(self.config))
        tty = self.root / 'ttyGS0'; tty.touch()
        pstore = self.root / 'pstore'; pstore.mkdir()
        state = self.udcs / 'fcc00000.usb/state'
        state.write_text('not attached\n')
        result = usb.status(config, self.gadgets, self.udcs, tty, pstore)
        self.assertTrue(result['tty_present'])
        self.assertFalse(result['enumerated'])
        state.write_text('configured\n')
        self.assertTrue(usb.status(config, self.gadgets, self.udcs, tty, pstore)['enumerated'])
        state.write_text('default\n')
        self.assertFalse(usb.status(config, self.gadgets, self.udcs, tty, pstore)['enumerated'])
