# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT/relative)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value
leds = module('rgb_tests', 'components/common/rootfs-overlay/usr/lib/armhpc/leds.py')
performance = module('performance_tests', 'components/common/rootfs-overlay/usr/lib/armhpc/performance.py')


class RGBTests(unittest.TestCase):
    def test_rescue_yellow_normal_green_fatal_red_and_optional_heartbeat(self):
        self.assertEqual(leds.colour('rescue'), {'red':1, 'green':1, 'blue':0, 'heartbeat':True})
        self.assertEqual(leds.colour('normal'), {'red':0, 'green':1, 'blue':0, 'heartbeat':True})
        self.assertEqual(leds.colour('error'), {'red':1, 'green':0, 'blue':0, 'heartbeat':False})
        self.assertFalse(leds.colour('rescue', False)['heartbeat'])

    def test_gpio_status_channels_do_not_change_phy_channels(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('red:status', 'green:status', 'blue:status', 'green:lan'):
                (root/name).mkdir()
                (root/name/'trigger').write_text('none\n')
                (root/name/'brightness').write_text('0\n')
            policy = {'channels':{c:c+':status' for c in ('red','green','blue')}}
            leds.apply('rescue', root=root, policy=policy)
            self.assertEqual((root/'red:status/brightness').read_text().strip(), '1')
            self.assertEqual((root/'green:status/brightness').read_text().strip(), '1')
            self.assertEqual((root/'blue:status/trigger').read_text().strip(), 'heartbeat')
            self.assertEqual((root/'green:lan/brightness').read_text().strip(), '0')
            leds.apply('error', root=root, policy=policy)
            self.assertEqual((root/'green:status/brightness').read_text().strip(), '0')
            self.assertEqual((root/'blue:status/trigger').read_text().strip(), 'none')

    def test_repeated_monitor_checks_do_not_restart_heartbeat(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with mock.patch.object(leds, 'STATUS', root/'status'), mock.patch.object(leds, 'FAULT', root/'fault'), \
                 mock.patch.object(leds, 'PREFERENCES', root/'preferences'), mock.patch.object(leds, 'base_mode', return_value='rescue'), \
                 mock.patch.object(leds, 'failed_units', return_value=[]), \
                 mock.patch.object(leds, 'apply', side_effect=lambda mode, heartbeat, **kw: {'mode':mode,'channels':leds.colour(mode, heartbeat)}) as output:
                leds.update({})
                leds.update({})
                self.assertEqual(output.call_count, 1)

    def oesp_policy(self):
        return json.loads((ROOT/'adapters/boards/wxy-oesp-v22/rootfs-overlay/etc/armhpc/led.json').read_text())

    def test_oesp_rescue_and_normal_are_green_but_faults_remain_red(self):
        policy=self.oesp_policy()
        for mode in ('normal','rescue'):
            self.assertEqual(leds.colour(mode,policy['heartbeat'],policy),
                             {'red':0,'green':1,'blue':0,'heartbeat':False})
        self.assertEqual(leds.colour('error',False,policy),
                         {'red':1,'green':0,'blue':0,'heartbeat':False})
        self.assertEqual(leds.colour('rescue')['red'],1)

    def test_rescue_without_data_is_healthy_and_normal_still_requires_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            policy=self.oesp_policy();policy.pop('network')
            with mock.patch.object(leds,'STATUS',root/'status'),mock.patch.object(leds,'FAULT',root/'fault'), \
                 mock.patch.object(leds,'PREFERENCES',root/'preferences'), \
                 mock.patch.object(leds,'base_mode',return_value='rescue') as role, \
                 mock.patch.object(leds,'apply',side_effect=lambda mode, heartbeat, **kw: {
                     'mode':mode,'channels':leds.colour(mode,heartbeat,kw['policy'])}), \
                 mock.patch.object(leds.subprocess,'run',return_value=mock.Mock(stdout='')) as command:
                leds.update(policy,check_storage=True)
                command.assert_not_called()
                self.assertEqual(json.loads((root/'status').read_text())['mode'],'rescue')
                role.return_value='normal'
                leds.update(policy,check_storage=True)
                self.assertEqual(json.loads((root/'status').read_text())['mode'],'error')
                self.assertIn('DATA',json.loads((root/'status').read_text())['reason'])

    def test_oesp_panel_follows_either_port_and_never_writes_phy_leds(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);net=root/'net';hardware=root/'leds'
            policy=self.oesp_policy()
            for name in ('eth0','eth1'):
                (net/name).mkdir(parents=True);(net/name/'carrier').write_text('0\n')
            for name in ('green:lan','red:lan','mdio_mux-0.0:00:yellow:lan-0','mdio_mux-0.0:00:green:lan-1'):
                (hardware/name).mkdir(parents=True)
                (hardware/name/'trigger').write_text('unchanged')
                (hardware/name/'brightness').write_text('unchanged')
            with mock.patch.object(leds,'NET',net),mock.patch.object(leds,'LEDS',hardware):
                state=leds.update_network(policy,{})
                self.assertFalse(state['linked'])
                self.assertEqual((hardware/'green:lan/brightness').read_text().strip(),'0')
                self.assertEqual((hardware/'red:lan/brightness').read_text().strip(),'0')
                (net/'eth1/carrier').write_text('1\n')
                state=leds.update_network(policy,state)
                self.assertTrue(state['linked'])
                self.assertEqual((hardware/'green:lan/brightness').read_text().strip(),'1')
                self.assertEqual((hardware/'red:lan/brightness').read_text().strip(),'1')
                with mock.patch.object(leds,'write_channel',wraps=leds.write_channel) as output:
                    leds.update_network(policy,state)
                    output.assert_not_called()
                (net/'eth0/carrier').write_text('1\n');(net/'eth1/carrier').write_text('0\n')
                state=leds.update_network(policy,state)
                self.assertTrue(state['linked'])
                (net/'eth0/carrier').write_text('0\n')
                self.assertFalse(leds.update_network(policy,state)['linked'])
            for path in hardware.glob('mdio_mux*/*'):
                self.assertEqual(path.read_text(),'unchanged')

    def test_unreadable_carrier_is_reported_and_does_not_invent_a_link(self):
        with tempfile.TemporaryDirectory() as folder:
            state=leds.network_state(self.oesp_policy(),root=Path(folder))
            self.assertFalse(state['linked'])
            self.assertEqual(state['interfaces'],{'eth0':None,'eth1':None})
            self.assertEqual(len(state['warnings']),2)

    def test_unconfigured_blue_does_not_retry_healthy_oesp_outputs(self):
        self.assertFalse(leds.needs_retry({'red':'applied','green':'applied','blue':'unconfigured'}))
        self.assertTrue(leds.needs_retry({'red':'applied','green':'unavailable','blue':'unconfigured'}))

    def test_bad_colour_and_overlapping_panel_outputs_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'colour'):
            leds.colour('rescue',policy={'mode_colours':{'rescue':{'red':2}}})
        policy=self.oesp_policy();policy['network']['channels']['red']='green:power'
        with tempfile.TemporaryDirectory() as folder,self.assertRaisesRegex(ValueError,'distinct'):
            leds.network_state(policy,root=Path(folder))


class PerformanceTests(unittest.TestCase):
    def test_late_cpu_probe_is_retried_and_status_is_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cpu = root/'cpu'
            cpu.mkdir()
            report = root/'report.json'
            first = performance.collect(cpu, root/'devfreq')
            self.assertTrue(first['discovery_pending'])
            self.assertFalse(first['cpu_ready'])
            performance.save_status(report, first)
            policy = cpu/'policy0'
            policy.mkdir()
            values = {'cpuinfo_max_freq':'1992000', 'cpuinfo_min_freq':'100000',
                      'scaling_available_governors':'performance schedutil',
                      'scaling_min_freq':'100000','scaling_max_freq':'1992000','scaling_governor':'schedutil'}
            for name,value in values.items():
                (policy/name).write_text(value+'\n')
            second = performance.collect(cpu, root/'devfreq')
            performance.save_status(report, second)
            self.assertTrue(second['cpu_ready'])
            self.assertFalse(second['discovery_pending'])
            self.assertEqual(json.loads(report.read_text())['cpu'][0]['governor'],'performance')
            self.assertFalse((report.parent/'.report.json.partial').exists())

    def test_cpu_policy_uses_hardware_max_without_overclock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            values={'cpuinfo_max_freq':'1800000','cpuinfo_min_freq':'408000',
                    'scaling_available_governors':'ondemand performance','scaling_max_freq':'1200000',
                    'scaling_min_freq':'600000','scaling_governor':'ondemand'}
            for name,value in values.items(): (root/name).write_text(value+'\n')
            result=performance.apply_cpu(root)
            self.assertEqual(result['requested_max_khz'],1800000)
            self.assertEqual((root/'scaling_governor').read_text().strip(),'performance')
            self.assertEqual((root/'scaling_min_freq').read_text().strip(),'408000')

    def test_unavailable_governor_does_not_modify_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name,value in {'cpuinfo_max_freq':'1800000','cpuinfo_min_freq':'408000',
                               'scaling_available_governors':'ondemand','scaling_max_freq':'1200000'}.items():
                (root/name).write_text(value+'\n')
            self.assertFalse(performance.apply_cpu(root)['applied'])
            self.assertEqual((root/'scaling_max_freq').read_text().strip(),'1200000')

    def test_devfreq_clears_userspace_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            for name,value in {'available_governors':'performance simple_ondemand','min_freq':'1',
                               'max_freq':'100','governor':'simple_ondemand'}.items():
                (root/name).write_text(value+'\n')
            self.assertTrue(performance.apply_devfreq(root)['applied'])
            self.assertEqual((root/'max_freq').read_text().strip(),'0')
            self.assertEqual((root/'governor').read_text().strip(),'performance')
