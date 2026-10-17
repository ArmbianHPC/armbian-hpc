# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import base64
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

LIB=Path(__file__).resolve().parents[1]/'components/common/rootfs-overlay/usr/lib/armhpc'
sys.path.insert(0,str(LIB))
import config_io
import node_state
import network_config
import network_ready
import auth_config
from admin import parse_public_key
import test_host_settings as host


class StateRecovery(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.data=self.root/'data'
        self.data.mkdir()
        self.mount=mock.patch.object(node_state,'data_mount',return_value=(self.data,{'uuid':'data-one','serial':'disk-one','wwn':None}))
        self.mount.start();self.addCleanup(self.mount.stop)
        self.run=mock.patch.object(node_state,'run',return_value='')
        self.run.start();self.addCleanup(self.run.stop)
        self.manifest={'unit':'owned.service','enabled':True,'files':['usr/local/bin/owned','etc/systemd/system/owned.service']}
        for relative in self.manifest['files']:
            path=self.root/relative;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('owned bytes\n');path.chmod(0o700 if '/bin/' in relative else 0o600)

    def test_program_and_unit_are_recovered_from_exact_cached_bytes(self):
        node_state.register(self.root,self.manifest)
        for name in self.manifest['files']:(self.root/name).unlink()
        planned=node_state.reconcile(self.root)
        self.assertEqual(len(planned['changes']),2)
        self.assertFalse((self.root/self.manifest['files'][0]).exists())
        node_state.reconcile(self.root,True)
        self.assertTrue(node_state.status(self.root)['installed']['ready'])
        node_state.reconcile(self.root,True)
        self.assertEqual((self.root/self.manifest['files'][0]).read_text(),'owned bytes\n')

    def test_corrupted_cache_prevents_all_repairs(self):
        node_state.register(self.root,self.manifest)
        for resource in (self.data/node_state.RESOURCES).iterdir():resource.write_text('corrupt')
        (self.root/self.manifest['files'][0]).unlink()
        with self.assertRaises(config_io.ConfigError):node_state.reconcile(self.root,True)
        self.assertFalse((self.root/self.manifest['files'][0]).exists())

    def test_external_changes_and_symlink_targets_are_not_overwritten(self):
        node_state.register(self.root,self.manifest)
        (self.root/self.manifest['files'][0]).write_text('administrator change')
        with self.assertRaises(config_io.ConfigError):node_state.reconcile(self.root,True)
        (self.root/self.manifest['files'][0]).unlink()
        (self.root/self.manifest['files'][0]).symlink_to('/etc/passwd')
        with self.assertRaises(config_io.ConfigError):node_state.reconcile(self.root,True)

    def test_unmounted_data_is_rejected_before_creating_state(self):
        self.mount.stop()
        with mock.patch.object(node_state,'run',return_value=json.dumps({'filesystems':[]})):
            result=node_state.status(self.root)
        self.assertFalse(result['installed']['ready'])
        self.assertFalse((self.data/'node-config').exists())

    def test_import_preview_and_invalid_manifest_have_no_persistent_side_effect(self):
        result=node_state.register(self.root,self.manifest,False)
        self.assertFalse(result['applied'])
        self.assertFalse((self.data/node_state.CATALOG).exists())
        self.assertFalse((self.data/node_state.RESOURCES).exists())
        invalid={**self.manifest,'files':[*self.manifest['files'],'etc/shadow']}
        with self.assertRaises(config_io.ConfigError):node_state.register(self.root,invalid)
        self.assertFalse((self.data/node_state.RESOURCES).exists())

    def test_declared_relative_program_alias_is_restored(self):
        alias=self.root/'usr/local/bin/alias';alias.symlink_to('owned')
        manifest={**self.manifest,'files':[*self.manifest['files'],'usr/local/bin/alias']}
        node_state.register(self.root,manifest)
        alias.unlink();node_state.reconcile(self.root,True)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(os.readlink(alias),'owned')

    def test_permission_drift_is_reported_without_accepting_new_program_bytes(self):
        node_state.register(self.root,self.manifest)
        (self.root/self.manifest['files'][0]).chmod(0o777)
        self.assertFalse(node_state.status(self.root)['installed']['ready'])
        with self.assertRaises(config_io.ConfigError):node_state.reconcile(self.root,True)

    def test_recovery_binds_the_system_to_data_even_if_another_disk_has_no_catalog(self):
        node_state.register(self.root,self.manifest)
        self.mount.stop()
        def command(*args,**kwargs):
            if args[0]=='findmnt':
                return json.dumps({'filesystems':[{'target':str(self.data),'source':'/dev/sda2',
                    'fstype':'ext4','uuid':'different-data','options':'rw,noatime'}]})
            raise AssertionError('Changed DATA must fail before examining resources')
        with mock.patch.object(node_state,'run',side_effect=command):
            with self.assertRaises(config_io.ConfigError):node_state.data_mount(self.root)

    def test_runtime_queues_registered_enabled_units_after_preparation(self):
        node_state.register(self.root,self.manifest)
        state=json.loads((self.data/node_state.CATALOG).read_text())
        with mock.patch.object(node_state,'load',return_value=(self.data,self.data/node_state.CATALOG,state)),\
             mock.patch.object(node_state,'inventory',return_value=([],[])),mock.patch.object(node_state,'run') as command:
            node_state.prepare_runtime()
        self.assertEqual(command.call_args_list[-1],mock.call('systemctl','start','--no-block','owned.service',timeout=5))


class ManagementTransactions(unittest.TestCase):
    setUp=host.HostSettingsTests.setUp
    submit=host.HostSettingsTests.submit
    static=host.HostSettingsTests.static
    def test_management_policy_roundtrip_and_rollback(self):
        value=network_config.settings('eth0',address='192.168.100.20/24',gateway='192.168.100.1',
                                      management=True,management_target='192.168.100.10',metric=80,
                                      route_table=200,source='192.168.100.20/32')
        identifier=self.submit(value)
        self.assertEqual(self.net.record()['proposed'],value)
        current=network_ready.policy(self.root)
        self.assertEqual(current['interface'],'eth0')
        rendered=network_config.render(value)
        self.assertIn('RequiredFamilyForOnline=ipv4',rendered)
        self.assertIn('Table=200',rendered)
        self.net.rollback(identifier)
        self.assertEqual(network_ready.policy(self.root)['family'],'ipv4')
        self.assertFalse((self.root/'etc/armhpc/management-network.json').exists())

    def test_optional_interface_has_no_wait_and_no_unwanted_gateway(self):
        value=network_config.settings('eth1',dhcp=True,metric=200,default_route=False)
        text=network_config.render(value)
        self.assertIn('RequiredForOnline=no',text)
        self.assertIn('UseGateway=no',text)
        self.assertNotIn('[DHCPv6]\nRouteMetric=200\nUseGateway=',text)

    def test_route_source_must_match_static_address(self):
        with self.assertRaises(config_io.ConfigError):
            network_config.settings('eth0',address='192.168.100.20/24',gateway='192.168.100.1',
                                     route_table=200,source='10.0.0.1/32')

    def test_ipv4_readiness_does_not_query_dhcpv6_state(self):
        def command(*args,**kwargs):
            self.assertNotIn('-6',args)
            if 'address' in args:return json.dumps([{'addr_info':[{'local':'192.168.100.20','scope':'global'}]}])
            return json.dumps([{'dev':'eth0','prefsrc':'192.168.100.20'}])
        with mock.patch.object(network_ready,'run',side_effect=command):
            self.assertTrue(network_ready.inspect({'interface':'eth0','family':'ipv4','management_target':'192.168.100.10'},self.root)['ready'])


class AuthConfirmation(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.config=self.root/'admin.conf';self.config.write_text('PasswordAuthentication yes\nAuthenticationMethods any\nUsePAM yes\n')
        self.key='ssh-ed25519 '+base64.b64encode(struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)+b'a'*32).decode()
        self.auth=self.root/'auth';self.auth.write_text('publickey '+self.key+'\n')
        self.record={'schema_version':1,'transaction_id':'id','stage':'key-check',
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'deadline':time.clock_gettime(time.CLOCK_BOOTTIME)+90,'fingerprint':parse_public_key(self.key)[1],
            'initiator_session':'client:1:server:22','confirmed_sessions':[],
            'backup':base64.b64encode(self.config.read_bytes()).decode(),
            'managed_hashes':[hashlib.sha256(self.config.read_bytes()).hexdigest()]}
        for patch in (mock.patch.object(auth_config,'state',return_value=self.record),
                      mock.patch.object(auth_config,'save'),mock.patch.object(auth_config,'run',return_value=''),
                      mock.patch.object(auth_config,'safe_path',return_value=self.config),
                      mock.patch.object(auth_config,'effective',return_value={'passwordauthentication':'no','kbdinteractiveauthentication':'no','authenticationmethods':'publickey','usepam':'yes'})):
            patch.start();self.addCleanup(patch.stop)

    def test_two_distinct_publickey_sessions_are_required(self):
        with mock.patch.dict(os.environ,{'SSH_CONNECTION':'client 2 server 22','SSH_USER_AUTH':str(self.auth)}):
            auth_config.confirm('id')
            self.assertEqual(self.record['stage'],'policy-check')
            with self.assertRaises(config_io.ConfigError):auth_config.confirm('id')
        with mock.patch.dict(os.environ,{'SSH_CONNECTION':'client 3 server 22','SSH_USER_AUTH':str(self.auth)}):
            auth_config.confirm('id')
        self.assertEqual(self.record['stage'],'confirmed')
        self.assertIn('UsePAM yes',self.config.read_text())

    def test_password_or_original_session_cannot_confirm(self):
        for connection,proof in (('client 1 server 22','publickey '+self.key),('client 2 server 22','password')):
            self.auth.write_text(proof+'\n')
            with mock.patch.dict(os.environ,{'SSH_CONNECTION':connection,'SSH_USER_AUTH':str(self.auth)}):
                with self.assertRaises(config_io.ConfigError):auth_config.confirm('id')

    def test_expired_transaction_restores_prior_policy(self):
        self.record['deadline']=0
        self.config.write_text(auth_config.change_text(self.config.read_text(),True))
        self.record['managed_hashes'].append(hashlib.sha256(self.config.read_bytes()).hexdigest())
        with mock.patch.object(auth_config,'configuration_lock',return_value=nullcontext()):auth_config.recover()
        self.assertEqual(self.record['stage'],'rolled-back')
        self.assertIn('PasswordAuthentication yes',self.config.read_text())

    def test_external_policy_edit_is_not_overwritten(self):
        self.config.write_text('external policy\n')
        with self.assertRaises(config_io.ConfigError):auth_config.rollback(self.record)


if __name__=='__main__':unittest.main()
