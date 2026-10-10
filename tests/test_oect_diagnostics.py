# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

source = Path(__file__).resolve().parents[1] / 'adapters/boards/wxy-oect-original/rootfs-overlay/usr/lib/armhpc/oect-boot-diagnostics.py'
spec = importlib.util.spec_from_file_location('diagnostics', source)
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


@unittest.skipUnless(os.name == 'posix', 'Linux boot helper')
class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.net = self.root / 'net'
        self.net.mkdir()
        self.leds = self.root / 'leds'
        for color in ('red', 'blue', 'green'):
            (self.leds / (color + ':')).mkdir(parents=True)
            (self.leds / (color + ':') / 'trigger').write_text('heartbeat' if color == 'blue' else 'none')
            (self.leds / (color + ':') / 'brightness').write_text('1' if color in ('red','green') else '0')
        net = patch.object(diagnostics, 'NET', self.net)
        commands = patch.object(diagnostics, 'command', return_value={'exit_code': 0, 'output': 'fixture'})
        for mock in (net, commands):
            mock.start()
            self.addCleanup(mock.stop)

    def test_missing_hardware_reports_absence_without_overriding_rgb_mode(self):
        state = diagnostics.snapshot()
        self.assertEqual(state['network_state'], 'no-hardware-ethernet')
        self.assertEqual((self.leds / 'red:/brightness').read_text().strip(), '1')
        self.assertEqual((self.leds / 'green:/brightness').read_text().strip(), '1')
        self.assertEqual((self.leds / 'blue:/trigger').read_text().strip(), 'heartbeat')

    def test_unplugged_ethernet_is_distinct_from_missing_driver(self):
        ethernet = self.net / 'eth0'
        (ethernet / 'device').mkdir(parents=True)
        for key, value in {'type': '1', 'carrier': '0', 'operstate': 'down'}.items():
            (ethernet / key).write_text(value)
        self.assertEqual(diagnostics.snapshot()['network_state'], 'no-carrier')
        self.assertEqual((self.leds / 'red:/brightness').read_text().strip(), '1')
        (ethernet / 'carrier').write_text('1')
        self.assertEqual(diagnostics.snapshot()['network_state'], 'carrier-present')

    def test_history_is_bounded_and_latest_is_preserved(self):
        directory = self.root / 'logs'
        for number in range(6):
            boot_id = f'{number:08x}-0000-0000-0000-000000000000'
            diagnostics.save(directory, boot_id, [{'network_state': 'fixture'}])
        paths = list(directory.glob('boot-????????-????-????-????-????????????.json'))
        self.assertEqual(len(paths), 4)
        latest = directory / 'boot-diagnostics-latest.json'
        self.assertEqual(json.loads(latest.read_text())['boot_id'], boot_id)
        self.assertEqual(latest.stat().st_mode & 0o777, 0o600)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_rejects_large_reports_and_symlink_destination(self):
        with self.assertRaises(RuntimeError):
            diagnostics.save(self.root / 'logs', '00000000-0000-0000-0000-000000000000', ['x' * 600000])
        target = self.root / 'target'
        target.mkdir()
        link = self.root / 'link'
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(RuntimeError):
            diagnostics.save(link, '00000000-0000-0000-0000-000000000000', [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
