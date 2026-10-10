# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import copy
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from framework.boot_environment import (ensure_environment_fits, inspect_environment,
                                        require_environment_component, environment_component,
                                        verify_environment_only_change)
from framework.config import ConfigError


class PersistentEnvironmentTests(unittest.TestCase):
    def setUp(self):
        variables = b'bootcmd=run bootcmd_emmc\0bootcmd_emmc=sysboot mmc 0:1 any 0xc00000 /extlinux/extlinux.conf\0serial#=opaque-value\0\0'
        body = variables.ljust(508, b'\0')
        self.data = struct.pack('<I', zlib.crc32(body)) + body
        digest = hashlib.sha256(self.data).hexdigest()
        self.spec = {'schema_version': 1, 'format': 'uboot-env-crc32-le', 'offset_bytes': 1024,
                     'size_bytes': 512, 'record_size_bytes': 512, 'sha256': digest,
                     'records': [{'sha256': digest, 'expected_variables': {'bootcmd': 'run bootcmd_emmc',
                                  'bootcmd_emmc': 'sysboot mmc 0:1 any 0xc00000 /extlinux/extlinux.conf'}}]}

    def test_boot_commands_validated_without_exporting_vendor_values(self):
        result = inspect_environment(self.data, self.spec)
        self.assertTrue(result['passed'])
        self.assertNotIn('serial#', str(result))
        self.assertNotIn('opaque-value', str(result))

    def test_bad_crc_rejected_even_when_payload_hash_is_registered(self):
        corrupted = b'\0' * 4 + self.data[4:]
        spec = copy.deepcopy(self.spec)
        spec['sha256'] = spec['records'][0]['sha256'] = hashlib.sha256(corrupted).hexdigest()
        with self.assertRaisesRegex(ConfigError, 'CRC32'):
            inspect_environment(corrupted, spec)

    def test_wrong_partition_boot_command_is_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec['records'][0]['expected_variables']['bootcmd_emmc'] = 'sysboot mmc 0:7 any 0xc00000 /extlinux/extlinux.conf'
        with self.assertRaisesRegex(ConfigError, 'boot commands'):
            inspect_environment(self.data, spec)

    def test_environment_cannot_overlap_boot_partition(self):
        with self.assertRaisesRegex(ConfigError, 'overlaps BOOT'):
            ensure_environment_fits(self.spec, 1024)
        ensure_environment_fits(self.spec, 1536)

    def test_idb_fit_inventory_without_environment_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, 'missing'):
            require_environment_component([{'role': 'idbloader'}, {'role': 'uboot'}], self.spec)
        require_environment_component([environment_component(self.spec)], self.spec)

    def test_missing_second_environment_record_is_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec['size_bytes'] = 1024
        spec['sha256'] = hashlib.sha256(self.data * 2).hexdigest()
        spec['records'] *= 2
        with self.assertRaisesRegex(ConfigError, 'incomplete'):
            inspect_environment(self.data, spec)

    def test_any_change_outside_declared_environment_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base, repaired = Path(temporary) / 'base.img', Path(temporary) / 'repaired.img'
            before = b'G' * 1024 + bytes(512) + b'B' * 2048
            base.write_bytes(before)
            repaired.write_bytes(before[:1024] + self.data + before[1536:])
            self.assertTrue(verify_environment_only_change(base, repaired, self.spec)['outside_environment_identical'])
            with repaired.open('r+b') as stream:
                stream.seek(1600)
                stream.write(b'!')
            with self.assertRaisesRegex(ConfigError, 'outside'):
                verify_environment_only_change(base, repaired, self.spec)


if __name__ == '__main__':
    unittest.main()
