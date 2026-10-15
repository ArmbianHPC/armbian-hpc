# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT/'adapters/boards/wxy-oesp-v22'
spec = importlib.util.spec_from_file_location('oesp_phy_leds', BOARD/'rootfs-overlay/usr/lib/armhpc/phy_leds.py')
phy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phy)


class OespLEDTests(unittest.TestCase):
    def fixture(self, root):
        policy = json.loads((BOARD/'rootfs-overlay/etc/armhpc/phy-led.json').read_text())
        node = root/'dt/phy'
        port = root/'net/eth0/phydev'
        port.mkdir(parents=True)
        node.mkdir(parents=True)
        (port/'of_node').symlink_to(node)
        (port/'phy_id').write_text(policy['phy_id'])
        for index, colour, role in ((1, 'green', 'link'), (2, 'yellow', 'activity')):
            led = root/'leds'/f'mdio_mux-0.0:00:{colour}:lan-{index}'
            led.mkdir(parents=True)
            child = node/'leds'/f'led@{index}'
            child.mkdir(parents=True)
            (child/'reg').write_bytes(struct.pack('>I', index))
            (led/'of_node').symlink_to(child)
            for name, value in {**phy.settings(role), 'trigger': 'none [netdev]', 'device_name': 'eth0', 'offloaded': 1}.items():
                (led/name).write_text(str(value)+'\n')
        return policy

    def test_status_reads_live_yellow_rules_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);policy=self.fixture(root)
            before={p:p.read_bytes() for p in (root/'leds').glob('*/*') if p.is_file()}
            report=phy.observe(policy,net_root=root/'net',led_root=root/'leds')
            self.assertTrue(report['configured'])
            self.assertEqual(report['source'],'live-sysfs')
            self.assertTrue(report['physical_mapping_confirmed'])
            self.assertEqual([(x['index'],x['role']) for x in report['outputs']],[(1,'link'),(2,'activity')])
            self.assertEqual(before,{p:p.read_bytes() for p in before})
            policy['mapping_confirmed'] = False
            self.assertFalse(phy.observe(policy,net_root=root/'net',led_root=root/'leds')['physical_mapping_confirmed'])
            (root/'leds/mdio_mux-0.0:00:yellow:lan-2/link_1000').write_text('1\n')
            report=phy.observe(policy,net_root=root/'net',led_root=root/'leds')
            self.assertFalse(report['configured'])
            self.assertEqual(report['outputs'][1]['settings']['link_1000'],1)

    def test_old_dtb_missing_activity_output_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);policy=self.fixture(root)
            (root/'leds/mdio_mux-0.0:00:yellow:lan-2/of_node').unlink()
            report=phy.observe(policy,net_root=root/'net',led_root=root/'leds')
            self.assertFalse(report['configured'])
            self.assertIn('Missing PHY LED output 2',report['reason'])

    def test_stopped_trigger_or_offload_is_not_reported_as_configured(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);policy=self.fixture(root)
            led=root/'leds/mdio_mux-0.0:00:yellow:lan-2'
            (led/'trigger').write_text('[none] netdev\n')
            self.assertFalse(phy.observe(policy,net_root=root/'net',led_root=root/'leds')['configured'])
            (led/'trigger').write_text('none [netdev]\n');(led/'offloaded').write_text('0\n')
            self.assertFalse(phy.observe(policy,net_root=root/'net',led_root=root/'leds')['configured'])
