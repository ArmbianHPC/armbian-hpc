# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import json
from pathlib import Path
import tempfile
import unittest
from framework.kernel_baseline import merge,validate,module_manifest


class KernelBaselineChecks(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        policy=self.root/'profiles/kernel/kubernetes.json';policy.parent.mkdir(parents=True)
        policy.write_text(json.dumps({'requirements':{'CONFIG_CFS_BANDWIDTH':['y'],'CONFIG_VXLAN':['y','m']}}))
        self.config=self.root/'.config'
        self.config.write_text('CONFIG_CFS_BANDWIDTH=y\nCONFIG_VXLAN=m\n')

    def test_final_config_rejects_a_cached_missing_capability(self):
        self.assertIn('config_sha256',validate(self.root,self.config))
        self.config.write_text('CONFIG_VXLAN=m\n')
        with self.assertRaisesRegex(ValueError,'CONFIG_CFS_BANDWIDTH'):validate(self.root,self.config)

    def test_merge_always_revisits_existing_config(self):
        commands=[]
        merge(self.root,self.root/'source',self.root,lambda *args:commands.append(args))
        self.assertEqual(commands[0][1:3],('--file',str(self.config)))
        self.assertIn('CFS_BANDWIDTH',commands[0]);self.assertIn('VXLAN',commands[0])
        self.assertEqual(self.config.read_text(),'CONFIG_CFS_BANDWIDTH=y\nCONFIG_VXLAN=m\n')

    def test_module_receipt_requires_matching_indexes_and_detects_content_changes(self):
        modules=self.root/'lib/modules/example';modules.mkdir(parents=True)
        (modules/'modules.dep').write_text('module.ko:\n')
        with self.assertRaises(ValueError):module_manifest(self.root,'example')
        (modules/'modules.builtin').write_text('')
        (modules/'module.ko').write_bytes(b'first')
        before=module_manifest(self.root,'example')
        (modules/'module.ko').write_bytes(b'second')
        self.assertNotEqual(before['sha256'],module_manifest(self.root,'example')['sha256'])


if __name__=='__main__':unittest.main()
