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


class PerformanceTests(unittest.TestCase):
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
