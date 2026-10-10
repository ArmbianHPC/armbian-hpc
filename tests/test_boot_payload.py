# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Catch headers which a raw-only loader would pass unchanged to Linux."""
from pathlib import Path
import tempfile
import unittest

from framework.boot_payload import raw_initramfs_format, verify_raw_initramfs


class RawInitramfsTests(unittest.TestCase):
    def test_supported_compressions_and_cpio_are_identified(self):
        for value, name in ((b'\x28\xb5\x2f\xfd', 'zstd'), (b'\xfd7zXZ\x00', 'xz'),
                            (b'\x1f\x8b', 'gzip'), (b'\x02\x21\x4c\x18', 'lz4'),
                            (b'070701', 'cpio'), (b'070702', 'cpio')):
            with self.subTest(name=name):
                self.assertEqual(raw_initramfs_format(value)[0], name)

    def test_legacy_ramdisk_header_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Legacy uImage'):
            raw_initramfs_format(b'\x27\x05\x19\x56'+bytes(60)+b'\x28\xb5\x2f\xfd')

    def test_unknown_and_truncated_headers_are_rejected(self):
        for value in (b'', b'\x28\xb5', b'notinitrd', b'0707'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                raw_initramfs_format(value)

    def test_kernel_must_support_the_selected_compression_before_root_mount(self):
        with tempfile.TemporaryDirectory() as folder:
            initrd, config = Path(folder)/'initrd', Path(folder)/'config'
            initrd.write_bytes(b'\x28\xb5\x2f\xfd'+bytes(32))
            config.write_text('CONFIG_BLK_DEV_INITRD=y\nCONFIG_RD_ZSTD=y\n')
            self.assertEqual(verify_raw_initramfs(initrd, config)['compression'], 'zstd')
            for text in ('CONFIG_BLK_DEV_INITRD=y\n# CONFIG_RD_ZSTD is not set\n',
                         'CONFIG_BLK_DEV_INITRD=y\nCONFIG_RD_ZSTD=m\n',
                         'CONFIG_RD_ZSTD=y\n'):
                config.write_text(text)
                with self.assertRaises(ValueError):
                    verify_raw_initramfs(initrd, config)
