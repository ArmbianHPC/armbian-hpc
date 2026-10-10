# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Disk-selection and confirmation gates; no real block devices are accessed."""
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('armhpc_rescue_tests', ROOT / 'components/rescue/rootfs-overlay/usr/lib/armhpc/rescue.py')
rescue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rescue)


class RescueTests(unittest.TestCase):
    def test_partition_plan_fixes_root_at_64_gib_and_uses_aligned_remaining_data(self):
        size = 128 * 1024**3 + 128 * 512
        root, data = rescue.partition_plan(size, 64 * 1024**3)
        self.assertEqual(root, {'start': 2048, 'size': 64 * 1024**3 // 512})
        self.assertEqual(data['start'], root['start'] + root['size'])
        self.assertLessEqual((data['start'] + data['size']) * 512 + 33 * 512, size)
        self.assertEqual(data['size'] * 512 % 1024**2, 0)

    def test_undersized_disk_is_rejected(self):
        with self.assertRaises(rescue.RescueError):
            rescue.partition_plan(64 * 1024**3, 64 * 1024**3)

    def select(self, info):
        info = {**info, 'path': '/dev/fixture'}
        def fake_run(*args, capture=False):
            if args[0] == 'lsblk':
                self.assertIn('--tree', args)
                self.assertIn('NAME', args[args.index('--output')+1].split(','))
                return json.dumps({'blockdevices': [info]})
            if args[0] == 'blockdev':
                return '512'
            self.fail('Selection unexpectedly reached ' + str(args))
        with mock.patch.object(Path, 'resolve', return_value=Path('/dev/fixture')), \
             mock.patch.object(Path, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
             mock.patch.object(rescue, 'run', side_effect=fake_run):
            return rescue.disk_info('/dev/fixture')

    def test_emmc_usb_partition_and_loop_are_refused(self):
        for kind, transport in (('disk', None), ('disk', 'usb'), ('part', 'sata'), ('loop', None)):
            with self.subTest(kind=kind, transport=transport), self.assertRaises(rescue.RescueError):
                self.select({'type': kind, 'tran': transport})

    def test_missing_serial_and_mounted_partition_are_refused(self):
        info = {'type': 'disk', 'tran': 'sata', 'serial': '', 'mountpoints': [None],
                'children': [], 'maj:min': '8:0', 'size': 128 * 1024**3}
        with self.assertRaises(rescue.RescueError):
            self.select(info)
        info.update(serial='fixture-serial', children=[{'mountpoints': ['/mounted']}])
        with self.assertRaisesRegex(rescue.RescueError, 'mounted'):
            self.select(info)

    def test_existing_unmounted_sata_partition_is_not_an_ambiguous_disk(self):
        info={'path':'/dev/fixture','type':'disk','tran':'sata','serial':'fixture-serial',
              'model':'fixture 240G','mountpoints':[None],'maj:min':'8:0','size':240057409536,
              'children':[{'path':'/dev/fixture1','type':'part','mountpoints':[None],'maj:min':'8:1'}]}
        def fake_run(*args, capture=False):
            if args[0]=='lsblk' and '--json' in args:
                self.assertIn('--tree',args)
                return json.dumps({'blockdevices':[info]})
            if args[0]=='lsblk': return '179:0 179:2'
            if args[0]=='blockdev': return '512'
            if args[0]=='findmnt': return '/dev/mmcblk0p2'
            if args[0]=='swapon': return ''
            self.fail(str(args))
        with mock.patch.object(Path,'resolve',return_value=Path('/dev/fixture')), \
             mock.patch.object(Path,'stat',return_value=SimpleNamespace(st_mode=stat.S_IFBLK)), \
             mock.patch.object(Path,'iterdir',side_effect=lambda:iter(())), \
             mock.patch.object(rescue,'run',side_effect=fake_run):
            result=rescue.disk_info('/dev/fixture')
        self.assertEqual(result['serial'],'fixture-serial')
        self.assertEqual(result['size'],240057409536)

    def test_incomplete_device_path_is_rejected_before_disk_discovery(self):
        with mock.patch.object(rescue,'run') as command:
            with self.assertRaises(rescue.RescueError): rescue.disk_info('/dv')
            command.assert_not_called()

    def invoke(self, *, execute=False, confirmation=None, change_disk=False):
        disk = {'path': '/dev/sda', 'serial': 'fixture-serial', 'size': 128 * 1024**3,
                'number': '8:0', 'model': 'test'}
        policy = {'root_size_bytes': 64 * 1024**3, 'root_label': 'TEST_ROOT', 'data_label': 'TEST_DATA'}
        def fake_run(*args, **kwargs):
            return '/dev/sda\n/dev/sda1\n/dev/sda2'
        with ExitStack() as stack:
            argv = ['armhpc-rescue install', '--disk', '/dev/sda', '--username', 'hpc', '--public-key-file', '/test-only.pub']
            if execute:
                argv.append('--execute')
            stack.enter_context(mock.patch.object(sys, 'argv', argv))
            stack.enter_context(mock.patch.object(rescue, 'load_policy', return_value=policy))
            stack.enter_context(mock.patch.object(rescue, 'assert_rescue'))
            stack.enter_context(mock.patch.object(rescue, 'check_factory'))
            stack.enter_context(mock.patch.object(rescue, 'check_administrator'))
            disks = [disk, {**disk, 'serial': 'different'}] if change_disk else None
            stack.enter_context(mock.patch.object(rescue, 'disk_info', side_effect=disks, return_value=disk))
            stack.enter_context(mock.patch.object(rescue, 'run', side_effect=fake_run))
            stack.enter_context(mock.patch.object(rescue.subprocess, 'run', return_value=SimpleNamespace(returncode=2, stdout='')))
            operation = stack.enter_context(mock.patch.object(rescue, 'do_install'))
            stack.enter_context(mock.patch.object(sys.stdin, 'isatty', return_value=True))
            stack.enter_context(mock.patch.object(sys.stdout, 'isatty', return_value=True))
            stack.enter_context(mock.patch('builtins.input', return_value=confirmation))
            stack.enter_context(mock.patch('builtins.open', mock.mock_open()))
            stack.enter_context(mock.patch.object(rescue, 'fcntl', SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=mock.Mock())))
            class TerminalOutput(io.StringIO):
                def isatty(self): return True
            stack.enter_context(redirect_stdout(TerminalOutput()))
            if execute and change_disk:
                with self.assertRaises(rescue.RescueError):
                    rescue.main('install')
            else:
                rescue.main('install')
            operation.assert_not_called()

    def test_default_preview_does_not_write(self):
        self.invoke()

    def test_wrong_confirmation_does_not_write(self):
        self.invoke(execute=True, confirmation='n')
        self.invoke(execute=True, confirmation='')

    def test_serial_change_after_confirmation_does_not_write(self):
        self.invoke(execute=True, confirmation='y', change_disk=True)

    def test_y_n_confirmation_accepts_explicit_yes_and_reprompts_invalid_input(self):
        disk={'path':'/dev/sda','model':'fixture','serial':'fixture','size':128*1024**3}
        for replies,expected in ((['Y'],True),(['yes'],True),(['n'],False),([''],False),(['invalid','n'],False)):
            with mock.patch('builtins.input',side_effect=replies),redirect_stdout(io.StringIO()):
                self.assertEqual(rescue.confirm_write('install',disk),expected)

    def test_running_root_requires_the_separate_local_rescue_guard(self):
        import runpy
        path = ROOT / 'components/common/rootfs-overlay/usr/lib/armhpc/admin.py'
        ns = runpy.run_path(str(path), run_name='guard_test')['main'].__globals__
        offline = mock.Mock(side_effect=ns['ProvisionError']('offline only'))
        local = mock.Mock(side_effect=ns['ProvisionError']('local console only'))
        with mock.patch.dict(ns, {'check_offline_root': offline, 'check_rescue_root': local}):
            with self.assertRaisesRegex(ns['ProvisionError'], 'offline only'):
                ns['provision'](Path('/'), 'hpc', Path('/test-only.pub'))
            offline.assert_called_once_with(Path('/'))
            local.assert_not_called()
            with self.assertRaisesRegex(ns['ProvisionError'], 'local console only'):
                ns['provision'](Path('/'), 'hpc', Path('/test-only.pub'), rescue=True)
            local.assert_called_once_with(Path('/'))


if __name__ == '__main__':
    unittest.main()
