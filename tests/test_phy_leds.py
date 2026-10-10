# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('phy_led_tests',ROOT/'adapters/boards/wxy-oect-original/rootfs-overlay/usr/lib/armhpc/phy_leds.py')
phy=importlib.util.module_from_spec(spec)
spec.loader.exec_module(phy)

class PHYLEDTests(unittest.TestCase):
    def test_link_rates_and_activity_rules_match_rtl8211f(self):
        link,active=phy.settings('link'),phy.settings('activity')
        self.assertEqual([link[k] for k in ('link_10','link_100','link_1000')],[1,1,1])
        self.assertEqual([link[k] for k in ('rx','tx','link')],[0,0,0])
        self.assertEqual([active[k] for k in ('link_10','link_100','link_1000')],[0,0,0])
        self.assertEqual([active[k] for k in ('rx','tx')],[1,1])

    def test_discovery_uses_phy_of_node_and_index(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); node=root/'phy'; classes=root/'classes'
            (node/'leds/led@1').mkdir(parents=True); classes.mkdir()
            (node/'leds/led@1/reg').write_bytes(struct.pack('>I',1))
            (classes/'driver:green:lan-1').mkdir()
            (classes/'driver:green:lan-1/of_node').symlink_to(node/'leds/led@1')
            (classes/'green:status').mkdir()
            self.assertEqual(phy.discover(classes,node),{1:classes/'driver:green:lan-1'})

    def test_configuration_requires_verified_hardware_offload(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name,value in {**dict.fromkeys(phy.settings('activity'),'0'),'trigger':'none','device_name':'','offloaded':'1'}.items():
                (root/name).write_text(value+'\n')
            self.assertTrue(phy.configure(root,'eth0','activity')['offloaded'])
            (root/'offloaded').write_text('0\n')
            with self.assertRaisesRegex(ValueError,'offload'):
                phy.configure(root,'eth0','link')

    def test_real_r7_sysfs_parent_mapping_without_child_of_node(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); node=root/'devtree/phy'; classes=root/'leds'
            node.mkdir(parents=True);classes.mkdir()
            for index,colour in ((0,'white'),(1,'green'),(2,'yellow')):
                led=classes/f'stmmac-0:00:{colour}:lan-{index}'
                (led/'device').mkdir(parents=True)
                (led/'device/of_node').symlink_to(node)
            unrelated=classes/'other:green:lan-1'
            (unrelated/'device').mkdir(parents=True)
            (unrelated/'device/of_node').symlink_to(root/'other-phy')
            found=phy.discover(classes,node)
            self.assertEqual(set(found),{0,1,2})
            self.assertEqual(found[1].name,'stmmac-0:00:green:lan-1')
