# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('clock_floor_tests', ROOT / 'components/common/rootfs-overlay/usr/lib/armhpc/clock_floor.py')
clock = importlib.util.module_from_spec(spec); spec.loader.exec_module(clock)
REAL_TRACKING = clock.tracking


class ClockRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, filename in {'BUILD':'build.json','SAVED':'local/confirmed.json','RESUME':'local/resume.json',
                               'STATUS':'run/status.json','LOCK':'run/clock.lock','SHARED_ROOT':'shared',
                               'SHARED':'shared/clock/confirmed-time.json','SHARED_RESUME':'shared/clock/resume-time.json'}.items():
            p = mock.patch.object(clock, name, self.root/filename); p.start(); self.addCleanup(p.stop)
        self.wall, self.mono, self.boot, self.ntp = 900, 10, 'boot-a', None
        patches = [mock.patch.object(clock,'boot_id',side_effect=lambda:self.boot),
                   mock.patch.object(clock.time,'time',side_effect=lambda:self.wall),
                   mock.patch.object(clock.time,'monotonic',side_effect=lambda:self.mono),
                   mock.patch.object(clock.time,'clock_settime',side_effect=lambda _,value:setattr(self,'wall',value)),
                   mock.patch.object(clock,'tracking',side_effect=lambda *a:self.ntp),
                   mock.patch.object(clock,'shared_clock_path',return_value=clock.SHARED),
                   mock.patch.object(clock,'clock_lock',side_effect=lambda:nullcontext())]
        for p in patches:p.start();self.addCleanup(p.stop)
        clock.save(clock.BUILD,{'epoch':800})
        clock.save(clock.SHARED,{'epoch':1000,'source':'chrony-confirmed-ntp'})

    def reboot(self):
        self.boot+='-next';self.wall=900;self.mono=1
        return clock.restore()

    def test_legacy_shared_record_restores_time(self):
        state=clock.restore();self.assertEqual(1000,self.wall);self.assertEqual('estimated',state['quality']);self.assertTrue(state['valid_anchor'])

    def test_flush_before_reboot_preserves_elapsed_minutes(self):
        clock.restore();self.mono=310;self.wall=1300
        result=clock.checkpoint(True,'ota-reboot',True)
        self.assertEqual(1300,result['last_saved_epoch']);self.assertFalse(result['ntp_synchronised'])
        self.reboot();self.assertEqual(1300,self.wall)

    def test_a_b_system_share_resume_and_confirmed_identity(self):
        clock.restore()
        for index in range(3):
            self.mono+=60;self.wall+=60;clock.checkpoint(True,'slot-change',True)
            before=self.wall;self.reboot();self.assertEqual(before,self.wall)

    def test_manual_future_wall_clock_is_not_persisted_as_calibrated(self):
        clock.restore();self.mono=40;self.wall=9000000
        clock.checkpoint(True,'shutdown',True)
        self.assertEqual(1030,clock.load(clock.SHARED_RESUME)['epoch'])
        self.assertEqual(1000,clock.load(clock.SHARED)['epoch'])

    def test_ntp_corrects_future_record_and_discards_stale_resume(self):
        clock.save(clock.SHARED,{'epoch':999999,'source':'chrony-confirmed-ntp'})
        clock.restore();clock.checkpoint(True,'before-correction',True)
        self.wall=2000;self.ntp={'reference':'CB6B0658'}
        result=clock.checkpoint(True,'ntp-correction',True)
        self.assertEqual('backward',result['correction']['direction'])
        self.assertEqual(2000,clock.load(clock.SHARED)['epoch'])
        self.reboot();self.assertEqual(2000,self.wall)

    def test_later_backward_ntp_correction_is_saved_immediately(self):
        clock.restore();self.wall=5000;self.ntp={'reference':'CB6B0658'};clock.checkpoint()
        self.wall=2000;self.mono+=30;clock.checkpoint()
        self.assertEqual(2000,clock.load(clock.SHARED)['epoch'])
        self.reboot();self.assertEqual(2000,self.wall)

    def test_authoritative_revision_wins_over_older_future_local_copy(self):
        clock.save(clock.SAVED,{'epoch':999999,'source':'chrony-confirmed-ntp'})
        clock.save(clock.SHARED,{'epoch':2000,'source':'chrony-confirmed-ntp','revision':1})
        clock.restore();self.assertEqual(2000,self.wall)

    def test_resume_with_wrong_anchor_is_rejected(self):
        clock.save(clock.SHARED_RESUME,{'epoch':99999,'anchor_id':'wrong'})
        clock.restore();self.assertEqual(1000,self.wall)

    def test_corrupt_shared_record_uses_valid_local_copy(self):
        clock.SHARED.write_text('{bad');clock.save(clock.SAVED,{'epoch':1200,'source':'chrony-confirmed-ntp'})
        result=clock.restore();self.assertEqual(1200,self.wall);self.assertTrue(result['warnings'])

    def test_missing_shared_mount_never_writes_hidden_shared_records(self):
        clock.save(clock.SAVED,{'epoch':1100,'source':'chrony-confirmed-ntp'})
        before=clock.SHARED.read_bytes()
        with mock.patch.object(clock,'shared_clock_path',return_value=None):
            clock.restore();self.mono+=50;clock.checkpoint(True,'offline',True)
        self.assertEqual(before,clock.SHARED.read_bytes());self.assertFalse(clock.SHARED_RESUME.exists())

    def test_build_only_does_not_authorize_ota(self):
        clock.SHARED.unlink();clock.restore()
        self.assertEqual('invalid',clock.snapshot()['quality'])
        with self.assertRaises(clock.ClockError):clock.checkpoint(True,'ota-start',True)

    def test_calibrated_record_remains_readable_by_old_integer_epoch_helper(self):
        clock.restore();self.wall=2000.75;self.ntp={'reference':'CB6B0658'};clock.checkpoint()
        saved=clock.load(clock.SHARED)
        self.assertIsInstance(saved['epoch'],int);self.assertEqual(2000,clock.floor_epoch({},saved))
        self.assertEqual(2000.75,clock.load(clock.SHARED_RESUME)['epoch'])

    def test_first_ntp_detection_persists_without_five_minute_delay(self):
        clock.restore();self.wall=2000;self.ntp={'reference':'CB6B0658'}
        clock.checkpoint();self.assertEqual(2000,clock.load(clock.SHARED)['epoch'])

    def test_real_csv_calibration_replaces_stale_resume_before_reboot(self):
        parser=TrackingTests()
        clock.restore();self.mono+=20;self.wall=2000.75
        with mock.patch.object(clock,'tracking',side_effect=lambda *a:parser.parse(parser.report())):
            result=clock.checkpoint(True,'ntp-detected',True)
        self.assertEqual('synchronised',result['quality'])
        self.assertEqual(2000,clock.load(clock.SHARED)['epoch'])
        self.assertEqual(2000.75,clock.load(clock.SHARED_RESUME)['epoch'])
        self.reboot();self.assertEqual(2000.75,self.wall)

    def test_periodic_checks_only_write_persistent_storage_every_five_minutes(self):
        clock.restore();self.wall=2000;self.ntp={'reference':'CB6B0658'};clock.checkpoint()
        revision=clock.load(clock.SHARED)['revision'];self.mono+=30;self.wall+=30;clock.checkpoint()
        self.assertEqual(revision,clock.load(clock.SHARED)['revision'])
        self.mono+=300;self.wall+=300;clock.checkpoint();self.assertGreater(clock.load(clock.SHARED)['revision'],revision)

    def test_ntp_loss_retains_valid_anchor_and_estimated_quality(self):
        clock.restore();self.wall=2000;self.ntp={'reference':'CB6B0658'};clock.checkpoint()
        self.ntp=None;self.mono+=20;self.wall+=20
        state=clock.checkpoint(True,'offline-reboot',True);self.assertEqual('estimated',state['quality']);self.assertTrue(state['valid_anchor'])

    def test_corrupt_runtime_metadata_falls_back_to_saved_time(self):
        clock.restore();state=clock.load(clock.STATUS);state['checked_monotonic']='corrupt';clock.save(clock.STATUS,state)
        result=clock.snapshot();self.assertEqual('estimated',result['quality']);self.assertTrue(result['valid_anchor'])
        self.assertIn('invalid_runtime_clock_status',result['warnings'])

    def test_ntp_timeout_is_bounded_and_returns_estimate(self):
        clock.restore()
        self.assertEqual('estimated',clock.wait_sync(0)['quality'])

    def test_wait_without_ntp_has_thirty_second_deadline(self):
        clock.restore()
        def sleep(seconds):self.mono+=seconds;self.wall+=seconds
        with mock.patch.object(clock.time,'sleep',side_effect=sleep):result=clock.wait_sync(30)
        self.assertEqual('estimated',result['quality']);self.assertLessEqual(self.mono,40)

    def test_wait_for_delayed_ntp_saves_immediately(self):
        clock.restore()
        def sleep(seconds):
            self.mono+=seconds;self.wall+=seconds
            if self.mono>=12:self.ntp={'reference':'CB6B0658'};self.wall=2000
        with mock.patch.object(clock.time,'sleep',side_effect=sleep):result=clock.wait_sync(30)
        self.assertEqual('synchronised',result['quality']);self.assertEqual(2000,clock.load(clock.SHARED)['epoch'])

    def test_old_sync_report_is_not_reused_as_live_ntp_confirmation(self):
        clock.restore();self.wall=2000;self.ntp={'reference':'CB6B0658'};clock.checkpoint()
        self.mono+=61;self.wall+=61
        self.assertEqual('estimated',clock.snapshot()['quality'])

    def test_clock_write_failure_is_not_silently_accepted(self):
        clock.restore()
        with mock.patch.object(clock,'save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):clock.checkpoint(True,'ota-reboot',True)

    def test_atomic_replace_failure_retains_previous_record(self):
        before=clock.SHARED.read_bytes()
        with mock.patch.object(clock.os,'replace',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):clock.save(clock.SHARED,{'epoch':2000})
        self.assertEqual(before,clock.SHARED.read_bytes());self.assertEqual([],list(clock.SHARED.parent.glob('.confirmed-time.json*')))

    def test_save_fsyncs_file_and_parent(self):
        with mock.patch.object(clock.os,'fsync',wraps=clock.os.fsync) as sync:
            clock.save(clock.SAVED,{'epoch':1000})
        self.assertGreaterEqual(sync.call_count,2 if clock.os.name!='nt' else 1)

    def test_symlink_record_is_not_followed(self):
        target=self.root/'other.json';target.write_text('secret')
        clock.SAVED.parent.mkdir(exist_ok=True);clock.SAVED.symlink_to(target)
        self.assertEqual({},clock.load(clock.SAVED))
        with self.assertRaises(clock.ClockError):clock.save(clock.SAVED,{'epoch':1000})
        self.assertEqual('secret',target.read_text())


class TrackingTests(unittest.TestCase):
    def report(self,reference='CB6B0658',stratum='3',ref='1791570000',correction='0.001',leap='Normal'):
        return ','.join([reference,'203.107.6.88',stratum,ref,correction,*(['0']*8),leap])

    def parse(self,text,rc=0):
        with mock.patch.object(clock.subprocess,'run',return_value=mock.Mock(returncode=rc,stdout=text)):
            return REAL_TRACKING()

    def test_valid_ntp_tracking(self):self.assertIsNotNone(self.parse(self.report()))
    def test_local_unsynchronised_and_excessive_correction_rejected(self):
        for text in (self.report(reference='7F7F0101'),self.report(reference='00000000'),self.report(stratum='0'),
                     self.report(correction='2'),self.report(correction='nan'),self.report(leap='Not synchronised'),'bad'):
            with self.subTest(text=text):self.assertIsNone(self.parse(text))
    def test_real_chronyc_csv_has_fourteen_columns(self):
        # chrony 4.x process_cmd_tracking prints reference ID AND source address.
        text='CB6B0658,203.107.6.88,3,1791565578.123456789,0.000475125,0.000652901,0.000534881,30.209,0.255,7.885,0.041308716,0.012271560,64.1,Normal'
        result=self.parse(text+'\n')
        self.assertEqual(14,len(text.split(',')))
        self.assertEqual('CB6B0658',result['reference'])
        self.assertEqual(3,result['stratum'])
        self.assertAlmostEqual(0.000475125,result['remaining_correction_seconds'])

    def test_ipv6_source_address_does_not_shift_numeric_columns(self):
        text=self.report().replace('203.107.6.88','2001:db8::1')
        self.assertEqual(3,self.parse(text)['stratum'])

    def test_missing_address_or_extra_csv_column_is_rejected(self):
        text=self.report()
        for malformed in (text.replace(',203.107.6.88',''),text+',extra'):
            with self.subTest(text=malformed):self.assertIsNone(self.parse(malformed))

    def test_tracking_rejects_nonfinite_negative_and_untrusted_fields(self):
        for malformed in (self.report(correction='inf'),self.report(correction='-1.01'),
                          self.report(ref='nan'),self.report(ref='0'),
                          self.report(stratum='16'),self.report(reference='not-hex!')):
            with self.subTest(text=malformed):self.assertIsNone(self.parse(malformed))

    def test_valid_report_with_failed_command_is_rejected(self):
        self.assertIsNone(self.parse(self.report(),rc=1))

    def test_read_timeout_treated_as_unsynchronised(self):
        with mock.patch.object(clock.subprocess,'run',side_effect=subprocess.TimeoutExpired('chronyc',2)):
            self.assertIsNone(clock.tracking())
    def test_unmounted_or_wrong_shared_mount_rejected(self):
        with mock.patch.object(clock.Path,'is_mount',return_value=False):self.assertIsNone(clock.shared_clock_path())
        with mock.patch.object(clock.Path,'is_mount',return_value=True),mock.patch.object(clock.subprocess,'run',return_value=mock.Mock(returncode=0,stdout='ext4 OTHER')):
            self.assertIsNone(clock.shared_clock_path())


@unittest.skipIf(clock.os.name=='nt','Linux flock is required')
class ProcessLockTests(unittest.TestCase):
    def test_concurrent_clock_writer_is_bounded_and_can_resume(self):
        import sys
        with tempfile.TemporaryDirectory() as name,mock.patch.object(clock,'LOCK',Path(name)/'clock.lock'):
            code='import fcntl,time;f=open('+repr(str(clock.LOCK))+',"a+b");fcntl.flock(f,fcntl.LOCK_EX);print("locked",flush=True);time.sleep(5)'
            child=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,text=True)
            try:
                self.assertEqual('locked',child.stdout.readline().strip())
                start=clock.time.monotonic()
                with self.assertRaises(clock.ClockError) as error:
                    with clock.clock_lock():pass
                self.assertEqual('clock_busy',error.exception.code);self.assertLess(clock.time.monotonic()-start,2)
            finally:child.terminate();child.communicate(timeout=3)
            with clock.clock_lock():pass


if __name__=='__main__':unittest.main()
