# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('r8_update_tests',ROOT/'tools/maintenance/apply-runtime.py')
update=importlib.util.module_from_spec(spec);spec.loader.exec_module(update)

class UpdateTests(unittest.TestCase):
    def test_atomic_replace_preserves_previous_file_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source';target=root/'target';backup=root/'backup'
            source.write_text('new');target.write_text('old')
            update.copy_atomic(source,target,backup,0o644)
            self.assertEqual(target.read_text(),'new');self.assertEqual(backup.read_text(),'old')
            link=root/'link';link.symlink_to(target)
            with self.assertRaises(ValueError):update.copy_atomic(source,link,root/'unused')

    def test_userspace_overlay_leaves_data_and_machine_identity_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'root';bundle=base/'bundle'
            (root/'data').mkdir(parents=True);(bundle/'overlay').mkdir(parents=True)
            (root/'data/sentinel').write_text('keep-data')
            (root/'etc/ssh').mkdir(parents=True)
            (root/'etc/machine-id').write_text('keep-identity')
            (root/'etc/ssh/ssh_host_ed25519_key').write_text('fixture-only')
            (bundle/'overlay/clock-epoch').write_text('clock floor')
            (bundle/'overlay/script.py').write_text('new script')
            manifest={'update_id':'fixture','clock_floor_epoch':1700000000,
                'clock_package':{'path':'fixture.deb','version':'fixture'},
                'overlay':[{'scope':'common','source':'overlay/clock-epoch','target':'usr/lib/clock-epoch','mode':0o644},
                           {'scope':'common','source':'overlay/script.py','target':'usr/lib/armhpc/example.py','mode':0o644}]}
            with mock.patch.object(update,'install_package'),mock.patch.object(update,'run'):
                update.update_root(root,bundle,manifest,False)
            self.assertEqual((root/'data/sentinel').read_text(),'keep-data')
            self.assertEqual((root/'etc/machine-id').read_text(),'keep-identity')
            self.assertEqual((root/'etc/ssh/ssh_host_ed25519_key').read_text(),'fixture-only')
            self.assertEqual((root/'usr/lib/armhpc/example.py').read_text(),'new script')
