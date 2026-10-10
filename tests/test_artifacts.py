# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Image delivery must preserve the IMG and reject corrupt or unexpected input."""
import hashlib
import json
import lzma
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from framework.artifacts import package_image
from framework.config import ConfigError


@unittest.skipUnless(shutil.which('xz'), 'xz-utils is required for image packaging')
class ImagePackagingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.image = self.root / 'test.img'
        self.payload = b'ARMHPC test image\n' * 1000 + bytes(1024**2)
        self.image.write_bytes(self.payload)
        self.digest = hashlib.sha256(self.payload).hexdigest()

    def tearDown(self):
        self.directory.cleanup()

    def test_round_trip_preserves_img_and_existing_checksums(self):
        unrelated = '0' * 64 + '  notes.txt\n'
        (self.root / 'SHA256SUMS').write_text(unrelated)
        report = package_image(self.image, expected_sha256=self.digest)
        self.assertEqual(self.image.read_bytes(), self.payload)
        self.assertEqual(lzma.decompress((self.root / 'test.img.xz').read_bytes()), self.payload)
        self.assertTrue(report['passed'])
        self.assertEqual(report['decoded_image']['sha256'], self.digest)
        self.assertEqual(json.loads((self.root / 'compression-verification.json').read_text()), report)
        sums = (self.root / 'SHA256SUMS').read_text()
        self.assertIn(unrelated, sums)
        self.assertIn(self.digest + '  test.img\n', sums)
        self.assertIn(report['compressed_image']['sha256'] + '  test.img.xz\n', sums)

    def test_unexpected_img_hash_produces_no_delivery(self):
        with self.assertRaisesRegex(ConfigError, 'SHA256'):
            package_image(self.image, expected_sha256='0' * 64)
        self.assertFalse((self.root / 'test.img.xz').exists())
        self.assertFalse((self.root / 'compression-verification.json').exists())
        self.assertEqual(self.image.read_bytes(), self.payload)

    def test_corrupt_compressor_output_is_never_published(self):
        def corrupt(*args, **kwargs):
            kwargs['stdout'].write(b'not an xz stream')
        with mock.patch('framework.artifacts.subprocess.run', side_effect=corrupt):
            with self.assertRaises(lzma.LZMAError):
                package_image(self.image, expected_sha256=self.digest)
        self.assertFalse((self.root / 'test.img.xz').exists())
        self.assertFalse((self.root / 'test.img.xz.building').exists())
        self.assertFalse((self.root / 'compression-verification.json').exists())
        self.assertFalse((self.root / 'SHA256SUMS').exists())

    def test_existing_compressed_output_is_preserved(self):
        companion = self.root / 'test.img.xz'
        companion.write_bytes(b'previous delivery')
        with self.assertRaisesRegex(ConfigError, '已存在'):
            package_image(self.image, expected_sha256=self.digest)
        self.assertEqual(companion.read_bytes(), b'previous delivery')


if __name__ == '__main__':
    unittest.main()
