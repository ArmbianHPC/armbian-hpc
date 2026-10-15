# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
for relative in ('components/common/rootfs-overlay/usr/lib/armhpc','components/k3s/rootfs-overlay/usr/lib/armhpc'):
    sys.path.insert(0,str(ROOT/relative))
import config_io
import firewall_config as firewall
import kube_artifacts as artifacts
import kube_registry as registry
import kube_runtime
import node_state
import swap_config


class KernelFeatures(unittest.TestCase):
    def test_optional_swap_is_separate_from_container_requirements(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            policy={'CONFIG_SECCOMP':['y'],'CONFIG_CFS_BANDWIDTH':['y'],'CONFIG_DM_CRYPT':['y','m']}
            target=root/'etc/image-framework/kernel-requirements.json'
            target.parent.mkdir(parents=True);target.write_text(json.dumps(policy))
            module=root/'usr/lib/modules/example';module.mkdir(parents=True)
            (root/'lib').symlink_to('usr/lib')
            (module/'config').write_text('CONFIG_SECCOMP=y\nCONFIG_CFS_BANDWIDTH=y\n')
            value=node_state.kernel_status(root,'example')
            self.assertFalse(value['ready'])
            self.assertTrue(value['containers_ready'])
            self.assertFalse(value['features']['encrypted_swap']['ready'])
            (module/'config').write_text('CONFIG_DM_CRYPT=y\n')
            self.assertFalse(node_state.kernel_status(root,'example')['containers_ready'])


class FirewallTransactions(unittest.TestCase):
    def setUp(self):
        self.value=firewall.settings('server-etcd',['192.168.100.0/24'],['10.10.0.0/24'],22)
        self.identifier='01234567-89ab-cdef-0123-456789abcdef'

    def test_rules_own_one_table_and_keep_required_channels(self):
        rules=firewall.render(self.value,self.identifier)
        self.assertIn('table inet armhpc',rules)
        self.assertNotIn('flush ruleset',rules)
        for text in ('iifname "lo"','established,related','tcp dport 22','tcp dport 6443',
                     'tcp dport 10250','udp dport 8472','tcp dport 2379-2380'):
            self.assertIn(text,rules)
        self.assertNotIn('30000-32767',rules)

    def test_current_ssh_client_must_remain_allowed(self):
        with mock.patch.dict(os.environ,{'SSH_CONNECTION':'172.16.0.9 1200 192.168.100.4 22'}):
            with self.assertRaises(config_io.ConfigError):
                firewall.settings('worker',['192.168.100.0/24'],['10.10.0.0/24'])

    def test_crash_between_kernel_write_and_receipt_can_roll_back_owned_intent(self):
        value={'transaction_id':self.identifier,'backup':'old table','previous':{'stage':'confirmed'}}
        with mock.patch.object(firewall,'current',return_value=firewall.render(self.value,self.identifier)),\
             mock.patch.object(firewall,'replace') as replace,mock.patch.object(firewall,'save'):
            firewall.rollback(value)
        replace.assert_called_once_with('old table')
        with mock.patch.object(firewall,'current',return_value='administrator changed table'):
            with self.assertRaises(config_io.ConfigError):firewall.rollback(value)

    def test_restarting_service_accepts_only_exact_confirmed_table(self):
        value={'stage':'confirmed','applied_hash':firewall.checksum('existing table'),'policy':self.value}
        with mock.patch.object(firewall,'state',return_value=value),mock.patch.object(firewall,'current',return_value='existing table'),\
             mock.patch.object(firewall,'replace') as replace:
            firewall.boot()
        replace.assert_not_called()


class SwapFiles(unittest.TestCase):
    def test_unknown_symlink_or_sparse_file_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);sparse=root/'sparse'
            with sparse.open('wb') as stream:stream.truncate(1024**2)
            sparse.chmod(0o600)
            with self.assertRaises(config_io.ConfigError):swap_config.validate_file(sparse,1024**2)
            link=root/'link';link.symlink_to(sparse)
            with self.assertRaises(config_io.ConfigError):swap_config.validate_file(link,1024**2)

    def test_degraded_kubelet_policy_is_restored_when_swap_recovers(self):
        with tempfile.TemporaryDirectory() as name:
            data=Path(name);value={'profile':'limited'}
            self.assertTrue(swap_config.write_kubelet(data,value,True))
            path=data/'k3s/agent/etc/kubelet.conf.d/90-armhpc-swap.conf'
            self.assertEqual(json.loads(path.read_text())['memorySwap']['swapBehavior'],'NoSwap')
            self.assertTrue(swap_config.write_kubelet(data,value))
            restored=json.loads(path.read_text())
            self.assertEqual(restored['memorySwap']['swapBehavior'],'LimitedSwap')
            self.assertFalse(restored['failSwapOn'])
            self.assertFalse(swap_config.write_kubelet(data,value))


class OfflineArtifacts(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.offline=self.root/'offline';self.offline.mkdir()
        self.data=self.root/'data';self.data.mkdir()
        self.version='v1.36.5+k3s1'
        elf=bytearray(64);elf[:4]=b'\x7fELF';elf[18:20]=(183).to_bytes(2,'little')
        (self.offline/'k3s-arm64').write_bytes(elf)
        (self.offline/'k3s-airgap-images-arm64.tar.zst').write_bytes(b'fixed version images')
        self.installer=self.root/'install.sh';self.installer.write_text('pinned installer')
        self.policy={'installer_arch':'arm64','data_dir':str(self.data/'k3s')}
        self.plan={'version':self.version,'download_source':{'artifact_url':'https://example.invalid/releases',
                                                         'offline_directory':str(self.offline)}}
        self.checksums()

    def checksums(self):
        (self.offline/'sha256sum-arm64.txt').write_text(''.join(
            hashlib.sha256((self.offline/name).read_bytes()).hexdigest()+'  '+name+'\n'
            for name in ('k3s-arm64','k3s-airgap-images-arm64.tar.zst')))

    def prepare(self):
        copy=artifacts.copy
        def redirected(source,target,mode=0o600):
            return copy(source,self.root/'installed-k3s' if str(target)=='/usr/local/bin/k3s' else target,mode)
        with mock.patch.object(artifacts,'data_mount',return_value=(self.data,{'uuid':'one'})),\
             mock.patch.object(artifacts,'run',return_value='k3s version '+self.version+' (example)'),\
             mock.patch.object(artifacts,'copy',side_effect=redirected),\
             mock.patch.object(artifacts,'fetch',side_effect=AssertionError('offline installation used network')):
            return artifacts.prepare(self.policy,self.plan,self.installer,hashlib.sha256(self.installer.read_bytes()).hexdigest())

    def test_exact_program_arch_and_airgap_are_retained_without_network(self):
        receipt=self.prepare()
        self.assertEqual(receipt['version'],self.version)
        self.assertEqual((self.root/'installed-k3s').read_bytes(),(self.offline/'k3s-arm64').read_bytes())
        self.assertTrue((self.data/'k3s/agent/images/k3s-airgap-images-arm64.tar.zst').is_file())
        self.assertTrue((self.data/'node-config/armhpc/k3s-artifacts'/self.version/'arm64/manifest.json').is_file())

    def test_corrupt_resource_is_rejected_before_install(self):
        (self.offline/'k3s-arm64').write_bytes(b'changed')
        with self.assertRaises(config_io.ConfigError):self.prepare()
        self.assertFalse((self.root/'installed-k3s').exists())

    def test_matching_hash_cannot_substitute_another_architecture(self):
        elf=bytearray((self.offline/'k3s-arm64').read_bytes());elf[18:20]=(40).to_bytes(2,'little')
        (self.offline/'k3s-arm64').write_bytes(elf);self.checksums()
        with self.assertRaises(config_io.ConfigError):self.prepare()
        self.assertFalse((self.root/'installed-k3s').exists())

    def test_duplicate_checksums_and_duplicate_yaml_keys_are_rejected(self):
        with self.assertRaises(config_io.ConfigError):artifacts.sums(('0'*64+'  k3s\n')*2)
        with self.assertRaises(config_io.ConfigError):kube_runtime.check_configuration('node-name: a\nnode-name: b\n')
        with self.assertRaises(config_io.ConfigError):kube_runtime.check_configuration('kubelet-arg: [address=0.0.0.0]\n')

    def test_release_paths_preserve_official_and_native_mirror_version_rules(self):
        self.assertEqual(artifacts.release_prefix({'artifact_url':'https://rancher-mirror.rancher.cn/k3s'},self.version),
                         'https://rancher-mirror.rancher.cn/k3s/v1.36.5-k3s1/')
        self.assertIn('/v1.36.5%2Bk3s1/',artifacts.release_prefix({'artifact_url':'https://github.com/k3s-io/k3s/releases/download'},self.version))
        self.assertIn('%2B',artifacts.release_prefix({'artifact_url':'https://rancher-mirror.rancher.cn.other.invalid/k3s'},self.version))


if __name__=='__main__':unittest.main()
