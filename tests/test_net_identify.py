# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Exercise PHY association, rules, restoration and real worker interruption."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'components/common/rootfs-overlay/usr/lib/armhpc/net_identify.py'
spec = importlib.util.spec_from_file_location('net_identify_checks', MODULE)
identify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identify)


class IdentificationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix='armhpc-identify-')
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.net, self.leds = self.root / 'net', self.root / 'leds'
        self.state, self.policy = self.root / 'state', self.root / 'phy-led.json'
        device, phy = self.root / 'device/ethernet', self.root / 'dt/phy'
        device.mkdir(parents=True)
        phy.mkdir(parents=True)
        port = self.net / 'eth0'
        (port / 'phydev').mkdir(parents=True)
        (port / 'device').symlink_to(device, target_is_directory=True)
        (port / 'phydev/of_node').symlink_to(phy, target_is_directory=True)
        (port / 'carrier').write_text('1\n')
        (port / 'speed').write_text('1000\n')
        self.leds.mkdir()
        self.defaults = {name: int(name in ('link_10', 'link_100', 'link_1000')) for name in identify.MODE_FIELDS}
        for index, colour in enumerate(('white', 'green', 'yellow')):
            led = self.leds / f'stmmac-0:00:{colour}:lan-{index}'
            (led / 'device').mkdir(parents=True)
            (led / 'device/of_node').symlink_to(phy, target_is_directory=True)
            for name, value in {**self.defaults, 'trigger': 'none [netdev]', 'device_name': 'eth0', 'offloaded': '1'}.items():
                (led / name).write_text(str(value) + '\n')
        (self.leds / 'green:status').mkdir()
        self.policy.write_text(json.dumps({'interface': 'eth0', 'identify_index': 1}))
        for name, value in (('NET', self.net), ('LEDS', self.leds), ('STATE', self.state), ('POLICY', self.policy)):
            patch = mock.patch.object(identify, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        identify.initialize_state()
        self.green = self.leds / 'stmmac-0:00:green:lan-1'

    def prepare(self, seconds=1, period_ms=200):
        data = {'protocol': 1, 'task_id': 'a' * 32, 'interface': 'eth0', 'boot_id': identify.boot_id(),
                'snapshot': identify.capture('eth0'), 'stage': 'queued', 'seconds': seconds,
                'period_ms': period_ms, 'restored': False}
        identify.save(identify.state_file('eth0'), data)
        return data

    def worker(self, data):
        code = '''import importlib.util,sys
from pathlib import Path
s=importlib.util.spec_from_file_location("worker",sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
r=Path(sys.argv[2]);m.NET=r/"net";m.LEDS=r/"leds";m.POLICY=r/"phy-led.json";m.STATE=r/"state"
sys.exit(m.main(["--interface","eth0","--worker",sys.argv[3]]))
'''
        process = subprocess.Popen([sys.executable, '-B', '-c', code, str(MODULE), str(self.root), data['task_id']],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if identify.load_state('eth0')['stage'] == 'running':
                return process
            if process.poll() is not None:
                self.fail(str(process.communicate()))
            time.sleep(0.02)
        self.fail('Worker did not start')

    def test_only_green_led_from_requested_phy_is_selected(self):
        other = self.leds / 'another:green:lan-1'
        (other / 'device').mkdir(parents=True)
        (other / 'device/of_node').symlink_to(self.root / 'dt/other')
        self.assertEqual(identify.select_led('eth0'), self.green)
        self.assertNotEqual(identify.select_led('eth0'), self.leds / 'green:status')

    def test_missing_or_ambiguous_green_led_is_not_guessed(self):
        (self.green / 'device/of_node').unlink()
        with self.assertRaisesRegex(ValueError, 'unique green'):
            identify.capture('eth0')
        (self.green / 'device/of_node').symlink_to(self.root / 'dt/phy')
        self.policy.write_text('{}')
        duplicate = self.leds / 'another:green:lan-5'
        (duplicate / 'device').mkdir(parents=True)
        (duplicate / 'device/of_node').symlink_to(self.root / 'dt/phy')
        with self.assertRaisesRegex(ValueError, 'unique green'):
            identify.capture('eth0')

    def test_identification_requires_valid_link_and_hardware_offload(self):
        (self.net / 'eth0/carrier').write_text('0')
        with self.assertRaisesRegex(ValueError, 'active link'):
            identify.capture('eth0')
        (self.net / 'eth0/carrier').write_text('1')
        (self.green / 'offloaded').write_text('0')
        with self.assertRaisesRegex(ValueError, 'hardware-offloaded'):
            identify.capture('eth0')

    def test_invalid_port_and_timing_are_rejected_before_service_launch(self):
        for name in ('../eth0', '.', 'lo', 'eth0;reboot'):
            with self.assertRaises(ValueError):
                identify.interface_name(name)
        with mock.patch.object(identify.subprocess, 'run') as run:
            for seconds, period in ((0, 1000), (601, 1000), (30, 0), (30, 5001)):
                with self.assertRaises(ValueError):
                    identify.start('eth0', seconds, period)
            run.assert_not_called()

    def test_regular_on_off_cycles_restore_original_rules_and_isolate_other_leds(self):
        import threading
        data = self.prepare()
        original = {p.relative_to(self.root): p.read_bytes() for parent in (self.net, self.leds)
                    for p in parent.rglob('*') if p.is_file()}
        values = []
        writer = identify.write
        def observe(path, value):
            if path == self.green / 'link_1000':
                values.append(value)
            writer(path, value)
        with mock.patch.object(identify, 'write', side_effect=observe):
            result = identify.flash('eth0', data['task_id'], threading.Event())
        self.assertEqual(result['stage'], 'completed')
        self.assertTrue(result['restored'])
        self.assertGreaterEqual(values.count(1), 4)
        self.assertGreaterEqual(values.count(0), 4)
        for path, before in original.items():
            self.assertEqual((self.root / path).read_bytes(), before, path)

    def test_link_loss_restores_rules_and_reports_failure(self):
        data = self.prepare()
        class Disconnected:
            def is_set(self):
                return False
            def wait(self, seconds):
                (identify.NET / 'eth0/carrier').write_text('0')
                return False
        result = identify.flash('eth0', data['task_id'], Disconnected())
        self.assertEqual(result['stage'], 'failed')
        self.assertTrue(result['restored'])
        self.assertIn('active link', result['error'])
        for name, value in self.defaults.items():
            self.assertEqual(identify.read(self.green / name), str(value))

    def test_unsupported_rules_or_readback_are_not_reported_as_success(self):
        data = self.prepare()
        import threading
        with mock.patch.object(identify, 'set_rules', side_effect=ValueError('hardware rejection')):
            result = identify.flash('eth0', data['task_id'], threading.Event())
        self.assertEqual(result['stage'], 'restore-failed')
        self.assertFalse(result['restored'])

    def test_paired_rx_tx_restore_accepts_only_verified_final_rules(self):
        import errno
        saved = dict.fromkeys(self.defaults, 0)
        saved.update(rx=1, tx=1)
        for name, value in saved.items():
            identify.write(self.green / name, value)
        writer = identify.write
        def paired(path, value):
            writer(path, value)
            if path.name in ('rx', 'tx') and identify.read(self.green / 'rx') != identify.read(self.green / 'tx'):
                raise OSError(errno.EOPNOTSUPP, 'RX and TX must be paired')
        with mock.patch.object(identify, 'write', side_effect=paired):
            identify.set_rules(self.green, saved)
        self.assertEqual({name: int(identify.read(self.green / name)) for name in saved}, saved)

    @unittest.skipUnless(sys.platform == 'linux', 'Requires POSIX signals and file locks')
    def test_sigterm_stops_real_worker_and_restores_led(self):
        data = self.prepare(seconds=10)
        process = self.worker(data)
        self.assertTrue(identify.busy('eth0'))
        process.terminate()
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        result = identify.status('eth0')
        self.assertEqual(result['stage'], 'stopped')
        self.assertTrue(result['restored'])
        self.assertFalse(result['active'])
        self.assertNotIn('snapshot', result)

    @unittest.skipUnless(sys.platform == 'linux', 'Requires POSIX signals and file locks')
    def test_duplicate_start_is_refused_and_sigkill_can_be_repaired(self):
        data = self.prepare(seconds=10)
        process = self.worker(data)
        with self.assertRaisesRegex(ValueError, 'already flashing'):
            identify.start('eth0', 10, 1000)
        process.kill()
        process.communicate(timeout=5)
        self.assertEqual(identify.status('eth0')['stage'], 'interrupted')
        result = identify.stop('eth0')
        self.assertEqual(result['stage'], 'stopped')
        self.assertTrue(result['restored'])
        self.assertEqual({name: int(identify.read(self.green / name)) for name in self.defaults}, self.defaults)

    def test_stale_boot_or_mismatched_request_never_changes_led(self):
        data = self.prepare()
        import threading
        with self.assertRaisesRegex(ValueError, 'no longer current'):
            identify.flash('eth0', 'b' * 32, threading.Event())
        data['boot_id'] = 'old-boot'
        identify.save(identify.state_file('eth0'), data)
        with self.assertRaisesRegex(ValueError, 'no longer current'):
            identify.flash('eth0', data['task_id'], threading.Event())
        self.assertEqual({name: int(identify.read(self.green / name)) for name in self.defaults}, self.defaults)


if __name__ == '__main__':
    unittest.main()
