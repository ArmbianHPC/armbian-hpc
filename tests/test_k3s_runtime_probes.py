# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
for relative in ('components/common/rootfs-overlay/usr/lib/armhpc','components/k3s/rootfs-overlay/usr/lib/armhpc'):
    sys.path.insert(0,str(ROOT/relative))
import kube_runtime


class K3sRuntimeProbes(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)
        state=self.folder/'state.json'
        state.write_text(json.dumps({'plan':{'role':'agent','version':'v1.36.5+k3s1'}}))
        self.policy={'state_file':str(state),'data_dir':'/data/k3s','configuration_dir':'/data/node-config/k3s'}

    def runner(self,arguments,**kwargs):
        if arguments[0]=='systemctl':return subprocess.CompletedProcess(arguments,0,'','')
        self.assertEqual(kwargs['env']['K3S_DATA_DIR'],'/data/k3s')
        self.assertEqual(kwargs['env']['K3S_CONFIG_FILE'],'/data/node-config/k3s/config.yaml')
        self.assertEqual(kwargs['env']['CRI_CONFIG_FILE'],'/data/k3s/agent/etc/crictl.yaml')
        if arguments[1]=='crictl':
            self.assertIn('/data/k3s/agent/etc/crictl.yaml',arguments)
            self.assertIn('--timeout=5s',arguments)
            return subprocess.CompletedProcess(arguments,0,json.dumps({'status':{'conditions':[{'type':'RuntimeReady','status':True}]}}),'')
        self.assertIn('/etc/rancher/k3s/k3s.yaml',arguments)
        return subprocess.CompletedProcess(arguments,0,'ok','')

    def test_agent_uses_managed_paths_and_requires_runtime_ready(self):
        with mock.patch.dict(os.environ,{'K3S_DATA_DIR':'/wrong','CRI_CONFIG_FILE':'/wrong'}),mock.patch.object(kube_runtime.subprocess,'run',side_effect=self.runner) as probe:
            value=kube_runtime.runtime(self.policy)
        self.assertTrue(value['ready']);self.assertIsNone(value['api_ready'])
        self.assertEqual(probe.call_count,2)

    def test_server_requires_its_explicit_api_and_cri(self):
        Path(self.policy['state_file']).write_text(json.dumps({'plan':{'role':'server-init'}}))
        with mock.patch.object(kube_runtime.subprocess,'run',side_effect=self.runner) as probe:
            value=kube_runtime.runtime(self.policy)
        self.assertTrue(value['ready']);self.assertTrue(value['api_ready']);self.assertEqual(probe.call_count,3)

    def test_slow_cli_startup_can_complete_without_claiming_timeout_is_ready(self):
        original=subprocess.run
        def slow(arguments,**kwargs):
            if arguments[0]=='systemctl':return self.runner(arguments,**kwargs)
            code='import json,os,time;assert os.environ["K3S_DATA_DIR"]=="/data/k3s";time.sleep(4.1);print(json.dumps({"status":{"conditions":[{"type":"RuntimeReady","status":True}]}}))'
            return original([sys.executable,'-B','-c',code],**kwargs)
        with mock.patch.object(kube_runtime.subprocess,'run',side_effect=slow):
            self.assertTrue(kube_runtime.runtime(self.policy)['ready'])

    def test_timeout_is_not_ready_and_has_a_bounded_diagnostic(self):
        def timeout(arguments,**kwargs):
            if arguments[0]=='systemctl':return self.runner(arguments,**kwargs)
            raise subprocess.TimeoutExpired(arguments,kwargs['timeout'],stderr='private debug output')
        with mock.patch.object(kube_runtime.subprocess,'run',side_effect=timeout):value=kube_runtime.runtime(self.policy)
        self.assertFalse(value['ready']);self.assertEqual(value['errors'],[{'check':'cri','code':'timeout','timeout_seconds':12}])
        self.assertNotIn('private',json.dumps(value))

    def test_invalid_or_negative_runtime_response_is_never_ready(self):
        for response in ('not-json','[]','{}','{"status":{"conditions":[{"type":"RuntimeReady","status":false}]}}'):
            with self.subTest(response=response),mock.patch.object(kube_runtime.subprocess,'run',side_effect=[subprocess.CompletedProcess([],0,'',''),subprocess.CompletedProcess([],0,response,'')]):
                self.assertFalse(kube_runtime.runtime(self.policy)['ready'])

    def test_inactive_service_never_probes_cri(self):
        with mock.patch.object(kube_runtime.subprocess,'run',return_value=subprocess.CompletedProcess([],3,'','')) as probe:
            self.assertFalse(kube_runtime.runtime(self.policy)['ready'])
        self.assertEqual(probe.call_count,1)


if __name__=='__main__':unittest.main()
