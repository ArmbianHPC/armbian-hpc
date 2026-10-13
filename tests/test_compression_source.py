# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Check explicit OTA source selection before compression starts."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools/build/compare-squashfs.py'
spec = importlib.util.spec_from_file_location('compression_comparison', SCRIPT)
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)


class CompressionSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.staging = self.root / 'rescue'
        self.staging.mkdir()
        (self.staging / 'content').write_text('root content\n')
        self.output = self.root / 'compression'
        self.env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        self.env.pop('PYTHONPATH', None)

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(SCRIPT), '--root', str(self.staging),
                               '--output', str(self.output), '--epoch', '1791849600', *arguments],
                              cwd=self.root, env=self.env, text=True, capture_output=True, timeout=15)

    def source(self, name):
        root = self.root / name
        library = root / 'src/armhpc_ota'
        library.mkdir(parents=True)
        (library / '__init__.py').write_text('')
        (library / 'squashfs.py').write_text('def staging_fingerprint(root):\n    return "' + name + '"\n')
        return root

    def test_missing_source_argument_fails_before_output_creation(self):
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn('--ota-source', result.stderr)
        self.assertNotIn('ModuleNotFoundError', result.stderr)
        self.assertFalse(self.output.exists())

    def test_invalid_source_checkout_fails_before_output_creation(self):
        result = self.cli('--ota-source', str(self.root))
        self.assertEqual(result.returncode, 2)
        self.assertIn('src/armhpc_ota/squashfs.py', result.stderr)
        self.assertFalse(self.output.exists())

    def test_nonexistent_source_checkout_fails_before_output_creation(self):
        result = self.cli('--ota-source', str(self.root / 'missing'))
        self.assertEqual(result.returncode, 2)
        self.assertIn('checkout does not exist', result.stderr)
        self.assertFalse(self.output.exists())

    def test_selected_source_overrides_installed_import_path(self):
        selected = self.source('selected')
        unrelated = self.source('unrelated')
        code = ('import importlib.util,json,sys;from pathlib import Path;'
                'sys.path.insert(0,sys.argv[3]);'
                's=importlib.util.spec_from_file_location("compare",sys.argv[1]);'
                'm=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
                'print(json.dumps(m.fingerprint(Path(sys.argv[4]),Path(sys.argv[2]))))')
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPT), str(selected),
                                 str(unrelated / 'src'), str(self.staging)],
                                cwd=self.root, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), 'selected')

    def test_a_preloaded_different_package_is_refused(self):
        selected = self.source('selected')
        unrelated = self.source('unrelated')
        code = ('import importlib.util,sys;from pathlib import Path;'
                'sys.path.insert(0,sys.argv[3]);from armhpc_ota import squashfs;'
                's=importlib.util.spec_from_file_location("compare",sys.argv[1]);'
                'm=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
                'm.fingerprint(Path(sys.argv[4]),Path(sys.argv[2]))')
        result = subprocess.run([sys.executable, '-c', code, str(SCRIPT), str(selected),
                                 str(unrelated / 'src'), str(self.staging)],
                                cwd=self.root, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('different OTA package is already loaded', result.stderr)


if __name__ == '__main__':
    unittest.main()
