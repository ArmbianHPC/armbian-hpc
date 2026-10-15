# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util
import json
from pathlib import Path
import struct
import zlib
import pytest

script=Path(__file__).parents[1]/'tools/oesp-usb-diagnose.py'
if not script.is_file():script=Path(__file__).with_name('oesp-usb-diagnose.py')
spec=importlib.util.spec_from_file_location('oesp_usb_diagnose',script)
diag=importlib.util.module_from_spec(spec);spec.loader.exec_module(diag)


@pytest.mark.parametrize('command',[
    'mmc write 20000000 0 800','store erase boot','disk_initial 1','save_setting','saveenv',
    'efuse write 0 1','mmc dev 1 0; saveenv','fatload mmc 1:1 20000000 unauthorized',
    'mmc read 20000000 0 801','mmc read 20000000 7fffffff 800'])
def test_diagnostic_rejects_writes_or_out_of_range_reads(command):
    assert not diag.allowed(command)
    class Device:
        def bulkCmd(self,*args,**kwargs):raise AssertionError('Unsafe command reached the device')
    with pytest.raises(ValueError,match='allowlist'):
        diag.USBReader(Device()).command(command)


def test_valid_read_commands_are_explicitly_bounded():
    assert diag.allowed('mmc read 20000000 100 800')
    assert diag.allowed('mmc dev 1 2')
    assert diag.allowed('fatload mmc 1:1 20000000 uInitrd.uboot')


def test_environment_report_filters_identity_and_credentials():
    payload=b'bootcmd=run armhpc_mmc_boot\0serial=private\0password=private\0ethaddr=private\0\0'
    payload+=b'\xff'*(65532-len(payload));block=struct.pack('<I',zlib.crc32(payload))+payload
    result=diag.environment(block)
    assert result['crc4_valid'] and result['boot_variables']=={'bootcmd':'run armhpc_mmc_boot'}
    assert 'private' not in json.dumps(result)
    bad=bytearray(block);bad[80]^=1
    assert not diag.environment(bytes(bad))['crc4_valid']


def test_erased_hardware_boot_area_is_not_accepted_as_fip():
    for value in (0,255):
        result=diag.boot_area(bytes((value,))*2097664)
        assert result['fip_looks_erased'] and not result['fip_matches_pinned']


def test_transfer_failure_preserves_partial_report_and_restores_user_access(tmp_path):
    class Reader:
        commands=[]
        def select(self,area):self.commands.append('mmc dev 1 '+str(area))
        def read(self,*args):raise IOError('USB disconnected')
    reader=Reader();directory=tmp_path/'capture'
    result=diag.capture(reader,directory,{'release':'r4'},{'before':'TPL'})
    assert not result['complete'] and not result['hardware_verified']
    assert result['user_access_restored'] and result['commands'][-1]=='mmc dev 1 0'
    assert (directory/'report.json').is_file()
    assert all(diag.allowed(command) for command in reader.commands)


def test_completed_capture_does_not_certify_hardware(tmp_path):
    class Reader:
        def __init__(self):self.commands=[];self.area=0
        def select(self,area):self.area=area;self.commands.append('mmc dev 1 '+str(area))
        def read(self,offset,length):
            data=bytearray(length)
            if self.area==0 and offset==0:
                struct.pack_into('<I',data,440,0xd78ec303);data[510:512]=b'\x55\xaa'
                for i,start in enumerate((204800,1253376,2301952,3350528)):
                    data[450+i*16]=0x0c if i==0 else 0x83
                    struct.pack_into('<II',data,454+i*16,start,1048576)
            if self.area==0 and offset==100*1024**2:
                data[71:82]=b'OESP_BOOT  ';data[82:90]=b'FAT32   '
            return bytes(data)
        def command(self,command):raise ValueError('optional register query unsupported')
        def file(self,name,path):path.write_bytes(b'fixture');return {'bytes':7,'sha256':diag.sha(b'fixture')}
    reference={'release':'r4','boot_files':{name:{'bytes':7,'sha256':diag.sha(b'fixture')} for name in diag.FILES}}
    result=diag.capture(Reader(),tmp_path/'capture',reference,{'before':'TPL'})
    assert result['complete'] and not result['hardware_verified']
    assert result['boot0']['fip_looks_erased'] and not result['boot0']['fip_matches_pinned']
    assert not result['explicit_storage_write_commands']
    assert result['vendor_mmc_initialization_side_effects_unverified']


def test_tpl_command_padding_and_timeout_are_sent_to_control_transfer():
    calls=[]
    class Control:
        def ctrl_transfer(self,**args):calls.append(args)
    class Device:
        dev=Control()
        def bulkCmdStat(self,timeout):
            assert timeout==5000
            return b'success\0'
    diag.USBReader(Device()).command('echo ARMHPC',timeout=5000)
    assert calls==[{'bmRequestType':0x40,'bRequest':0x34,'wValue':0,'wIndex':2,
                   'data_or_wLength':'    echo ARMHPC\0','timeout':5000}]


def test_early_connection_failure_leaves_a_durable_report(tmp_path,monkeypatch):
    import sys
    expected=tmp_path/'expected.json'
    expected.write_text(json.dumps({'board':'wxy-oesp-v22','release':'oesp-bookworm-1.1.0-20261013-06'}))
    monkeypatch.setattr(sys,'argv',['diag','--expected',str(expected),'--output',str(tmp_path/'reports'),'--load-ram'])
    monkeypatch.setattr(diag,'is_administrator',lambda:True)
    monkeypatch.setattr(diag,'other_tool_check',lambda:None)
    monkeypatch.setattr(diag,'backend',lambda value:(object(),'fixture-backend'))
    def disconnected(*args):raise OSError('USB I/O cancelled')
    monkeypatch.setattr(diag,'connect',disconnected)
    with pytest.raises(SystemExit) as error:diag.main()
    assert error.value.code==2
    report=json.loads(next((tmp_path/'reports').glob('*/report.json')).read_text())
    assert not report['complete'] and report['ranges']=={} and report['commands']==[]
    assert report['session_id'] and report['error']['phase']=='connecting'
    assert report['transport']['backend']=='fixture-backend'
    assert report['phase']=='failed' and report['events'][-1]['error_type']=='OSError'


def test_error_message_decodes_windows_chinese_without_byte_escapes():
    message='由于线程退出或应用程序请求，已中止 I/O 操作。'
    error=OSError(None,message.encode('gbk'))
    assert diag.error_text(error)==message


def test_command_disconnect_is_recorded_before_io(tmp_path):
    session=diag.CaptureSession(tmp_path/'capture',{'release':'r4'})
    class Control:
        def ctrl_transfer(self,**args):raise OSError('disconnected')
    class Device:dev=Control()
    reader=diag.USBReader(Device(),session.emit)
    session.data['commands']=reader.commands
    with pytest.raises(OSError):reader.synchronise()
    report=json.loads(session.path.read_text())
    assert report['commands']==['echo ARMHPC']
    assert report['phase']=='command-submitted'
    assert not report['complete'] and not report['explicit_storage_write_commands']


def test_amlc_event_numbers_preserve_persistent_event_order(tmp_path):
    import io
    from usb_diag_handoff import transfer_amlc
    class Image(io.BytesIO):
        def size(self):return len(self.getvalue())
    session=diag.CaptureSession(tmp_path/'capture',{'release':'r4'})
    transfer_amlc(Image(b'abc'),lambda:(3,0),lambda *a:None,lambda:object(),lambda:None,session.emit)
    report=json.loads(session.path.read_text())
    assert [e['sequence'] for e in report['events']]==list(range(1,len(report['events'])+1))
    assert any(e.get('amlc_sequence')==0 for e in report['events'])


def test_running_burning_tool_stops_before_opening_usb(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(diag.os,'name','nt')
    monkeypatch.setattr(diag.subprocess,'run',lambda *a,**k:SimpleNamespace(stdout=b'"USB_Burning_Tool.exe","123"'))
    with pytest.raises(ValueError,match='Close USB Burning Tool'):diag.other_tool_check()


def test_attach_only_refuses_non_tpl_before_any_ram_loading(monkeypatch):
    import sys
    from types import ModuleType
    class Soc:
        STAGE_MINOR_TPL=16
        stage_major=1
        stage_minor=8
        need_password=False
        def __init__(self,value):pass
        def __str__(self):return 'SPL'
    package=ModuleType('pyamlboot');package.__path__=[]
    socid=ModuleType('pyamlboot.socid');socid.SocId=Soc
    optimus=ModuleType('pyamlboot.optimus')
    for name,value in [('pyamlboot',package),('pyamlboot.socid',socid),('pyamlboot.optimus',optimus)]:
        monkeypatch.setitem(sys.modules,name,value)
    class Device:
        def identify(self):return b'fixture'
    monkeypatch.setattr(diag,'fresh_device',lambda *a:Device())
    with pytest.raises(ValueError,match='Attach-only requires'):
        diag.connect(object(),None,False,attach_only=True)


def test_tpl_control_synchronisation_precedes_bulk_commands():
    calls=[]
    class Control:
        def ctrl_transfer(self,**args):
            calls.append(args)
            if args['bRequest']==0x31:return b'success\0'
    class Device:
        dev=Control()
        def bulkCmdStat(self,timeout):return b'success\0'
    diag.USBReader(Device()).synchronise()
    assert [c['bRequest'] for c in calls]==[0x30,0x31,0x34]
    assert calls[0]['wIndex']==1 and calls[1]['wIndex']==0
    assert calls[0]['data_or_wLength']==calls[2]['data_or_wLength']=='    echo ARMHPC\0'
    assert all(c['timeout']==5000 for c in calls)


def test_stale_spl_session_cannot_select_unsigned_loader(monkeypatch):
    import sys
    from types import ModuleType
    class Soc:
        STAGE_MINOR_IPL=0
        STAGE_MINOR_TPL=16
        stage_major=1
        stage_minor=8
        need_password=False
        def __init__(self,value):pass
        def __str__(self):return 'SPL'
    package=ModuleType('pyamlboot');package.__path__=[]
    socid=ModuleType('pyamlboot.socid');socid.SocId=Soc
    optimus=ModuleType('pyamlboot.optimus')
    for name,value in [('pyamlboot',package),('pyamlboot.socid',socid),('pyamlboot.optimus',optimus)]:
        monkeypatch.setitem(sys.modules,name,value)
    class Device:
        def identify(self):return b'fixture'
    monkeypatch.setattr(diag,'fresh_device',lambda *a:Device())
    with pytest.raises(ValueError,match='previous SPL session'):
        diag.connect(object(),None,True)


def test_non_elevated_process_reports_permission_before_usb(tmp_path,monkeypatch):
    import sys
    monkeypatch.setattr(diag,'Path',type(tmp_path))
    monkeypatch.setattr(diag.os,'name','nt')
    expected=tmp_path/'expected.json'
    expected.write_text(json.dumps({'board':'wxy-oesp-v22','release':'oesp-bookworm-1.1.0-20261013-06'}))
    monkeypatch.setattr(sys,'argv',['diag','--expected',str(expected),'--output',str(tmp_path/'reports'),'--load-ram'])
    monkeypatch.setattr(diag,'is_administrator',lambda:False)
    monkeypatch.setattr(diag,'backend',lambda *a:pytest.fail('Unexpected USB backend load'))
    with pytest.raises(SystemExit) as error:diag.main()
    assert error.value.code==2
    report=json.loads(next((tmp_path/'reports').glob('*/report.json')).read_text())
    assert not report['host']['administrator']
    assert 'administrator' in report['error']['message']
    assert report['commands']==[] and not report['complete']


def test_ram_short_packets_are_accumulated_to_the_declared_length(monkeypatch):
    import sys
    from types import ModuleType
    payload=bytes(range(256))*20;remaining=bytearray(payload);calls=[];events=[]
    class Endpoint:
        bEndpointAddress=0x81
        def read(self,wanted,timeout):
            size=min(128,wanted,len(remaining));data=bytes(remaining[:size]);del remaining[:size]
            return data
    endpoint=Endpoint()
    util=ModuleType('usb.util');util.ENDPOINT_IN=0x80
    util.endpoint_direction=lambda value:value&0x80
    util.find_descriptor=lambda *a,**k:endpoint
    usb=ModuleType('usb');usb.__path__=[];usb.util=util
    monkeypatch.setitem(sys.modules,'usb',usb);monkeypatch.setitem(sys.modules,'usb.util',util)
    class Control:
        def get_active_configuration(self):return {(0,0):object()}
        def ctrl_transfer(self,**args):
            calls.append(args)
            if args['bRequest']==0x33:return struct.pack('<IIII',0xefe8,len(payload),0,0)
    class Device:
        dev=Control()
        def bulkCmdStat(self,timeout):return b'success\0'
    reader=diag.USBReader(Device(),lambda phase,**fields:events.append((phase,fields)))
    assert reader.ram(diag.BUFFER,len(payload))==payload
    assert not reader.input_pending and not remaining
    assert calls[0]['bRequest']==0x34 and calls[1]['bRequest']==0x33
    assert calls[1]['bmRequestType']==0xc0 and calls[1]['data_or_wLength']==16
    assert calls[0]['data_or_wLength']=='    upload mem 0x20000000 normal 0x1400\0'
    assert not any(call['bRequest']==0x12 for call in calls)
    assert events[-1][1]['received_bytes']==len(payload)


def test_incomplete_input_prevents_a_conflicting_restore_command(tmp_path,monkeypatch):
    class Reader(diag.USBReader):
        def select(self,area,timeout=15000):
            self.commands.append('mmc dev 1 '+str(area))
        def read(self,*args):
            self.input_pending=True
            raise diag.USBReadError('Truncated transfer',{'expected_bytes':1024,'received_bytes':512})
    reader=Reader(object())
    result=diag.capture(reader,tmp_path/'capture',{'release':'r4'},{'before':'TPL'})
    assert not result['complete'] and not result['user_access_restored']
    assert reader.commands==['mmc dev 1 0']
    assert result['error']['transfer']['received_bytes']==512
    assert 'Incomplete USB input' in result['restore_access_error']


@pytest.mark.parametrize('command',[
    'upload store 1 normal 0x200','upload mem 0x1000 normal 0x200',
    'upload mem 0x20000000 normal 0x100001','upload mem 0x27fffffe normal 0x4',
    'download mem 0x20000000 normal 0x200','upload mem 0x20000000 normal 0x200; saveenv'])
def test_tpl_upload_allowlist_rejects_other_media_and_invalid_ranges(command):
    assert not diag.allowed(command)


def test_probe_only_reports_scope_and_never_reads_larger_ranges(tmp_path):
    class Reader:
        def __init__(self):self.commands=[];self.reads=[]
        def select(self,area):self.commands.append('mmc dev 1 '+str(area))
        def read(self,offset,length):
            self.reads.append((offset,length))
            if (offset,length)!=(0,512):raise AssertionError('Probe tried a larger read')
            data=bytearray(512);data[510:]=b'\x55\xaa'
            return bytes(data)
    reader=Reader()
    report=diag.capture(reader,tmp_path/'capture',{'release':'r4'},{'after':'TPL'},probe_only=True)
    assert reader.reads==[(0,512)] and report['complete']
    assert report['capture_scope']=='probe' and report['probe']['mbr_valid']
    assert not report['hardware_verified'] and report['ranges']=={} and report['boot_files']=={}
    assert report['transport']['ram_read_protocol']=='tpl-upload-0x33'


class PartialCaptureReader:
    def __init__(self,reject_ept=False,reject_boot0=False,ept_disconnect=False,file_disconnect=False):
        self.commands=[];self.reads=[];self.files=[];self.area=0
        self.reject_ept=reject_ept;self.reject_boot0=reject_boot0
        self.ept_disconnect=ept_disconnect;self.file_disconnect=file_disconnect
    def select(self,area):self.area=area;self.commands.append('mmc dev 1 '+str(area))
    def read(self,offset,length):
        self.reads.append((self.area,offset,length))
        if offset==36*1024**2:
            assert length==4096
            if self.ept_disconnect:raise OSError('USB disconnected')
            if self.reject_ept:raise diag.VendorCommandRejected('mmc read 20000000 12000 8','failed:')
        if self.area==1 and self.reject_boot0:raise diag.VendorCommandRejected('mmc dev 1 1','failed:')
        data=bytearray(length)
        if self.area==0 and offset==0:
            data[510:512]=b'\x55\xaa'
            for index,start in enumerate((204800,1253376,2301952,3350528)):
                data[450+index*16]=0x0c if index==0 else 0x83
                struct.pack_into('<II',data,454+index*16,start,1048576)
        if offset==36*1024**2:data[:4]=b'MPT\0';struct.pack_into('<I',data,16,3)
        if self.area==0 and offset==100*1024**2:
            data[71:82]=b'OESP_BOOT  ';data[82:90]=b'FAT32   '
        return bytes(data)
    def command(self,command):raise diag.VendorCommandRejected(command,'failed: unsupported optional query')
    def file(self,name,path):
        self.files.append(name)
        if self.file_disconnect:raise OSError('USB disconnected during BOOT file')
        path.write_bytes(b'fixture');return {'bytes':7,'sha256':diag.sha(b'fixture')}


def capture_reference():
    return {'release':'r4','boot_files':{name:{'bytes':7,'sha256':diag.sha(b'fixture')} for name in diag.FILES}}


def test_ept_rejection_keeps_its_warning_and_continues_boot_collection(tmp_path):
    reader=PartialCaptureReader(reject_ept=True)
    report=diag.capture(reader,tmp_path/'capture',capture_reference(),{'after':'TPL'})
    assert report['complete'] and report['collection_finished'] and report['partial']
    assert not report['ept_table']['available']
    assert report['boot0']['available'] and report['boot1']['available']
    assert reader.files==list(diag.FILES)
    assert any(row['component']=='ept-table' and not row['required'] for row in report['collection_issues'])


def test_boot0_rejection_still_collects_boot1_and_actual_boot_files(tmp_path):
    reader=PartialCaptureReader(reject_boot0=True)
    report=diag.capture(reader,tmp_path/'capture',capture_reference(),{'after':'TPL'})
    assert not report['boot0']['available'] and report['boot1']['available']
    assert reader.files==list(diag.FILES) and report['complete'] and report['partial']
    assert report['ept_table']['magic_valid'] and report['ept_table']['partition_count']==3


def test_ept_transport_failure_is_not_reclassified_as_optional_rejection(tmp_path):
    reader=PartialCaptureReader(ept_disconnect=True)
    report=diag.capture(reader,tmp_path/'capture',capture_reference(),{'after':'TPL'})
    assert not report['complete'] and report['error']['type']=='OSError'
    assert not reader.files and 'boot0' not in report


def test_boot_file_transport_failure_stops_further_file_requests(tmp_path):
    reader=PartialCaptureReader(file_disconnect=True)
    report=diag.capture(reader,tmp_path/'capture',capture_reference(),{'after':'TPL'})
    assert not report['complete'] and reader.files==[diag.FILES[0]]
    assert any(row['required'] for row in report['collection_issues'])


@pytest.mark.parametrize('command',[
    'mmc read 20000000 12000 800','mmc read 20000000 12020 1','mmc read 20000000 1221f 2',
    'store disprotect key','store disprotect all'])
def test_capture_does_not_enter_the_reserved_key_range_or_unlock_it(command):
    assert not diag.allowed(command)


def test_ept_prefix_request_ends_before_the_reserved_key_range():
    assert diag.allowed('mmc read 20000000 12000 8')
    assert 0x12000+8 < (36*1024**2+16384)//512


def test_boot_hash_mismatches_cannot_finish_as_success(tmp_path):
    class Reader(PartialCaptureReader):
        def file(self,name,path):
            self.files.append(name);path.write_bytes(b'wrong')
            return {'bytes':5,'sha256':diag.sha(b'wrong')}
    reader=Reader()
    report=diag.capture(reader,tmp_path/'capture',capture_reference(),{'after':'TPL'})
    assert report['collection_finished'] and not report['complete']
    assert report['error']['type']=='BOOTFileMismatch'
    assert len([row for row in report['collection_issues'] if row['required']])==4


def test_boot_file_read_uses_directory_size_without_filesize_macro(tmp_path,monkeypatch):
    calls=[]
    class Volume:
        def __init__(self,read,start,length):
            assert start==100*1024**2 and length==512*1024**2
        def copy_file(self,name,path,progress):
            calls.append(name);path.write_bytes(b'captured')
            return {'bytes':8,'directory_bytes':8,'sha256':diag.sha(b'captured')}
    monkeypatch.setattr(diag,'FAT32ReadOnly',Volume)
    reader=diag.USBReader(object())
    def command(*args):pytest.fail('BOOT file capture must not use filesize or fatload')
    reader.command=command
    assert reader.file('boot.scr',tmp_path/'boot.scr')['bytes']==8
    assert calls==['boot.scr']

