# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Check real option routing, role separation and retired command removal."""
from contextlib import redirect_stderr,redirect_stdout
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
COMMON=ROOT/'components/common/rootfs-overlay/usr/lib/armhpc'

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

cli=load('runtime_cli_test',COMMON/'cli.py')
layout=load('runtime_layout_test',COMMON/'command_layout.py')

class DispatchTests(unittest.TestCase):
    def test_default_help_is_available_without_privileged_work(self):
        for function,command in ((cli.normal_main,'armhpc'),(cli.rescue_main,'armhpc-rescue'),(cli.kube_main,'armhpc-kube')):
            out=io.StringIO()
            with mock.patch.object(cli.runpy,'run_path') as run,redirect_stdout(out):
                self.assertEqual(function([]),0)
                run.assert_not_called()
            self.assertIn(command,out.getvalue())

    def test_role_mismatch_and_unknown_subcommand_do_not_dispatch(self):
        for marker,fn,args in ((True,cli.normal_main,['led']), (False,cli.rescue_main,['install']),
                               (True,cli.kube_main,['check']), (False,cli.normal_main,['phy-led'])):
            with mock.patch.object(cli,'RESCUE_MARKER',mock.Mock(is_file=lambda:marker)), \
                 mock.patch.object(cli.runpy,'run_path') as run,redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:fn(args)
                self.assertEqual(error.exception.code,2);run.assert_not_called()

    def test_normal_arguments_and_prog_are_preserved_and_restored(self):
        argv=sys.argv
        def run(path,run_name,init_globals):
            self.assertEqual(Path(path),COMMON/'admin.py')
            self.assertEqual(run_name,'__main__')
            self.assertEqual(init_globals,{'ARMHPC_PROG':'armhpc firstrun'})
            self.assertEqual(sys.argv,['armhpc firstrun','--rootfs','/mount with spaces','--username','hpc','--public-key-file','/run/key.pub'])
        with mock.patch.object(cli,'RESCUE_MARKER',mock.Mock(is_file=lambda:False)),mock.patch.object(cli.runpy,'run_path',side_effect=run):
            cli.normal_main(['firstrun','--rootfs','/mount with spaces','--username','hpc','--public-key-file','/run/key.pub'])
        self.assertIs(sys.argv,argv)

    def test_rescue_install_routes_exact_action_and_execute_without_shell(self):
        module=ROOT/'components/rescue/rootfs-overlay/usr/lib/armhpc'
        entry=mock.Mock(return_value=7)
        with mock.patch.object(cli,'LIBRARY',module),mock.patch.object(cli,'RESCUE_MARKER',mock.Mock(is_file=lambda:True)), \
             mock.patch.object(cli.runpy,'run_path',return_value={'entry':entry}):
            self.assertEqual(cli.rescue_main(['install','--disk','/dev/sda','--execute']),7)
        entry.assert_called_once_with('install',['--disk','/dev/sda','--execute'])

    def test_kube_check_help_does_not_read_policy_or_disks(self):
        module=ROOT/'components/k3s/rootfs-overlay/usr/lib/armhpc'
        with mock.patch.object(cli,'LIBRARY',module),mock.patch.object(cli,'RESCUE_MARKER',mock.Mock(is_file=lambda:False)),redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as error:cli.kube_main(['check','--help'])
            self.assertEqual(error.exception.code,0)
        self.assertIn('armhpc-kube check',out.getvalue())

    def test_led_help_restores_argv_after_system_exit(self):
        argv=sys.argv
        with mock.patch.object(cli,'RESCUE_MARKER',mock.Mock(is_file=lambda:False)),redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as error:cli.normal_main(['led','--help'])
            self.assertEqual(error.exception.code,0)
        self.assertIs(sys.argv,argv);self.assertIn('armhpc led',out.getvalue())

    def test_identification_arguments_route_in_both_environments(self):
        argv=sys.argv
        for rescue,fn,program in ((False,cli.normal_main,'armhpc'),(True,cli.rescue_main,'armhpc-rescue')):
            def run(path,run_name,init_globals):
                self.assertEqual(Path(path),COMMON/'net_identify.py')
                self.assertEqual(sys.argv,[program+' identify','--interface','eth0','--seconds','30','--json'])
                self.assertEqual(init_globals,{'ARMHPC_PROG':program+' identify'})
            with mock.patch.object(cli,'RESCUE_MARKER',mock.Mock(is_file=lambda:rescue)),mock.patch.object(cli.runpy,'run_path',side_effect=run):
                fn(['identify','--interface','eth0','--seconds','30','--json'])
            self.assertIs(sys.argv,argv)

    def test_host_settings_route_exact_arguments_only_in_normal_system(self):
        for command,module,args in (
                ('hostname','hostname_config.py',['set','compute-07','--yes','--json']),
                ('network','network_config.py',['set','--interface','eth0','--dhcp','--plan','--json'])):
            with mock.patch.object(cli,'RESCUE_MARKER',mock.Mock(is_file=lambda:False)),mock.patch.object(cli.runpy,'run_path') as run:
                cli.normal_main([command,*args])
            self.assertEqual(Path(run.call_args.args[0]),COMMON/module)
            self.assertEqual(run.call_args.kwargs['init_globals'],{'ARMHPC_PROG':'armhpc '+command})
            self.assertNotIn(command,cli.RESCUE)

class LayoutTests(unittest.TestCase):
    def root(self):
        parent=ROOT/'build/tests';parent.mkdir(parents=True,exist_ok=True)
        fixture=tempfile.TemporaryDirectory(prefix='command-layout-',dir=parent)
        root=Path(fixture.name).resolve();self.addCleanup(fixture.cleanup)
        self.assertTrue(root.is_relative_to(parent.resolve()))
        for directory in ('usr/local/bin','usr/local/sbin'):(root/directory).mkdir(parents=True)
        for name in (*layout.RETIRED,*layout.GROUPS):(root/'usr/local/sbin'/name).write_text('fixture\n')
        return root

    def test_normal_keeps_only_two_groups_and_unrelated_files(self):
        root=self.root();unrelated=root/'usr/local/sbin/other-tool';unrelated.write_text('keep')
        result=layout.finalize(root,'normal')
        self.assertEqual(result['entrypoints'],['armhpc','armhpc-kube'])
        self.assertEqual(sorted(p.name for p in (root/'usr/local/sbin').glob('armhpc*')),['armhpc','armhpc-kube'])
        self.assertEqual(unrelated.read_text(),'keep')
        self.assertEqual((root/'usr/local/bin/armhpc').readlink(),Path('../sbin/armhpc'))

    def test_rescue_has_only_its_group_and_pruning_is_idempotent(self):
        root=self.root();layout.finalize(root,'rescue');layout.finalize(root,'rescue')
        self.assertEqual([p.name for p in (root/'usr/local/sbin').glob('armhpc*')],['armhpc-rescue'])
        self.assertEqual((root/'usr/local/bin/armhpc-rescue').readlink(),Path('../sbin/armhpc-rescue'))

    def test_owned_k3s_dropins_migrate_without_changing_other_lines(self):
        root=self.root()
        for service in ('k3s','k3s-agent'):
            p=root/'etc/systemd/system'/(service+'.service.d/10-image-framework-storage.conf')
            p.parent.mkdir(parents=True)
            p.write_text('[Unit]\nRequiresMountsFor=/data\n[Service]\nExecStartPre=/usr/local/sbin/armhpc-k3s-check\nEnvironment=KEEP=yes\n')
        layout.finalize(root,'normal')
        for p in (root/'etc/systemd/system').rglob('*.conf'):
            self.assertIn('ExecStartPre=/usr/local/sbin/armhpc-kube check\n',p.read_text())
            self.assertIn('Environment=KEEP=yes\n',p.read_text())


    def test_reused_rescue_getty_and_motd_migrate_without_legacy_alias(self):
        root=self.root()
        path=root/'etc/systemd/system/serial-getty@.service.d/10-armhpc-rescue.conf'
        path.parent.mkdir(parents=True)
        path.write_text('[Service]\nExecStart=\nExecStart=-/sbin/agetty --login-program /usr/local/sbin/armhpc-rescue-console --autologin root --noclear %I 1500000 vt100\nEnvironment=KEEP=yes\n')
        motd=root/'etc/motd';motd.write_text('ARMHPC independent Rescue.\nUse armhpc-rescue-configuration or armhpc --help.\n')
        result=layout.finalize(root,'rescue')
        import shlex
        argv=shlex.split(next(x.split('=',1)[1] for x in path.read_text().splitlines() if x.startswith('ExecStart=-')))
        self.assertEqual('/usr/local/sbin/armhpc-rescue',argv[argv.index('--login-program')+1])
        self.assertEqual('console',argv[argv.index('--login-options')+1])
        self.assertIn('1500000',argv);self.assertIn('vt100',argv)
        self.assertIn('Environment=KEEP=yes',path.read_text())
        self.assertFalse((root/'usr/local/sbin/armhpc-rescue-console').exists())
        self.assertIn('armhpc-rescue configuration',motd.read_text())
        self.assertIn(path.relative_to(root).as_posix(),result['migrated_resources'])
        before=path.read_bytes();layout.finalize(root,'rescue');self.assertEqual(before,path.read_bytes())

    def test_rescue_resource_symlink_escape_is_refused(self):
        root=self.root()
        outside=root.parent/('outside-'+root.name+'.conf')
        outside.write_text('untouched');self.addCleanup(outside.unlink)
        path=root/'etc/systemd/system/serial-getty@.service.d/10-armhpc-rescue.conf'
        path.parent.mkdir(parents=True);path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError,'escapes root'):layout.finalize(root,'rescue')
        self.assertEqual('untouched',outside.read_text())

    def test_custom_rescue_login_options_are_not_guessed(self):
        root=self.root()
        path=root/'etc/systemd/system/serial-getty@.service.d/10-armhpc-rescue.conf'
        path.parent.mkdir(parents=True)
        text='[Service]\nExecStart=/sbin/agetty --login-program /usr/local/sbin/armhpc-rescue-console --login-options custom\n'
        path.write_text(text)
        with self.assertRaisesRegex(ValueError,'Custom Rescue login options'):layout.finalize(root,'rescue')
        self.assertEqual(text,path.read_text())

    def test_command_directory_escape_is_refused(self):
        root=self.root();base=root/'usr/local/bin';base.rmdir();base.symlink_to(root.parent,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'escapes root'):layout.finalize(root,'normal')

    def test_phy_service_uses_private_module_and_public_wrapper_is_absent(self):
        unit=ROOT/'adapters/boards/wxy-oect-original/rootfs-overlay/etc/systemd/system/armhpc-oect-phy-leds.service'
        self.assertIn('ExecStart=/usr/bin/python3 /usr/lib/armhpc/phy_leds.py',unit.read_text())
        self.assertFalse((unit.parents[3]/'usr/local/sbin/armhpc-phy-led').exists())
