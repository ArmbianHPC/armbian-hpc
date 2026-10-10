# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Malformed vendor storage, device identity stability and hostname checks."""
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('armhpc_firstboot_tests', ROOT/'components/common/rootfs-overlay/usr/lib/armhpc/firstboot.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def slot(mac=b'\x00\x11\x22\x33\x44\x55', version=1):
    raw = bytearray(65536)
    struct.pack_into('<IIHHHH', raw, 0, tool.VENDOR_TAG, version, 0, 1, 64, 64440)
    struct.pack_into('<HHHH', raw, 16, 3, 0, len(mac), 0)
    raw[1024:1024+len(mac)] = mac
    struct.pack_into('<I', raw, 65532, version)
    return raw


class IdentityTests(unittest.TestCase):
    def test_valid_stored_lan_address_and_two_port_index(self):
        raw = slot(bytes.fromhex('0011223344550266778899aa'))
        self.assertEqual(tool.vendor_record(raw, 0)['mac'], '00:11:22:33:44:55')
        self.assertEqual(tool.vendor_record(raw, 1)['mac'], '02:66:77:88:99:aa')

    def test_bad_tag_torn_version_excess_items_and_out_of_bounds_are_rejected(self):
        for offset, value, fmt in ((0, 0, '<I'), (65532, 2, '<I'), (10, 127, '<H'), (18, 65535, '<H')):
            raw = slot()
            struct.pack_into(fmt, raw, offset, value)
            with self.subTest(offset=offset):
                self.assertIsNone(tool.vendor_record(raw))
        self.assertIsNone(tool.vendor_record(slot()[:-1]))

    def test_zero_and_multicast_vendor_addresses_are_unusable(self):
        for mac in (bytes(6), bytes.fromhex('ffffffffffff'), bytes.fromhex('011122334455')):
            self.assertIsNone(tool.vendor_record(slot(mac))['mac'])

    def test_vendor_has_priority_and_saved_random_survives_a_reboot(self):
        saved = {'mac': '02:11:22:33:44:55', 'origin': 'random', 'emmc_cid': 'device-a'}
        self.assertEqual(tool.choose_identity('00:12:34:56:78:9a', saved, 'device-a')[0], '00:12:34:56:78:9a')
        self.assertEqual(tool.choose_identity(None, saved, 'device-a'), ('02:11:22:33:44:55', 'persisted-random'))
        with mock.patch.object(tool, 'random_mac', return_value='02:66:77:88:99:aa') as generate:
            self.assertEqual(tool.choose_identity(None, saved, 'device-b')[0], '02:66:77:88:99:aa')
            generate.assert_called_once()

    def test_random_address_is_local_unicast_even_for_adversarial_random_bytes(self):
        for raw in (bytes(6), bytes([255])*6):
            with mock.patch.object(tool.secrets, 'token_bytes', return_value=raw):
                mac = tool.random_mac()
                self.assertTrue(tool.valid_mac(mac))
                self.assertEqual(int(mac[:2], 16) & 3, 2)

    def test_hostname_removes_separators_and_normalizes_case(self):
        self.assertEqual(tool.rescue_hostname('02:AB:CD:EF:12:A4'), 'armhpc-rescue-12a4')
        self.assertEqual(tool.normal_hostname('02:AB:CD:EF:12:A4'), 'armhpc-12a4')
        with self.assertRaises(ValueError):
            tool.rescue_hostname('01:11:22:33:44:55')

    def test_both_root_modes_set_hostname_and_hosts_from_the_same_mac(self):
        for rescue,prefix in ((True,'armhpc-rescue-'),(False,'armhpc-')):
            with mock.patch.object(Path,'is_file',return_value=rescue), \
                 mock.patch.object(Path,'read_text',return_value='127.0.0.1 localhost\n127.0.1.1 oldname\n'), \
                 mock.patch.object(tool,'atomic_write') as write, \
                 mock.patch.object(tool.socket,'sethostname') as sethost:
                self.assertEqual(tool.configure_hostname('02:11:22:33:ce:58'),prefix+'ce58')
                sethost.assert_called_once_with((prefix+'ce58').encode())
                self.assertIn(prefix+'ce58',write.call_args_list[-1].args[1])

    def test_raw_storage_chooses_newest_complete_slot_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            disk = Path(directory)/'emmc-fixture'
            with disk.open('wb') as stream:
                stream.truncate(tool.VENDOR_START+4*65536)
                stream.seek(tool.VENDOR_START)
                stream.write(slot(version=1))
                stream.write(slot(bytes.fromhex('0266778899aa'), 3))
            original = disk.read_bytes()
            with mock.patch.object(Path, 'exists', return_value=False):
                result = tool.stored_vendor_mac(disk)
            self.assertEqual(result, ('02:66:77:88:99:aa', 'rockchip-emmc-vendor-storage'))
            self.assertEqual(disk.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
