#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Capture OESP boot evidence through the existing Amlogic USB download driver."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import time
import uuid
import zlib
import logging
import subprocess
sys.path.insert(0,str(Path(__file__).resolve().parent))
from usb_diag_handoff import transfer_amlc, wait_for_tpl, emit
from usb_diag_stream import USBReadError
from usb_diag_upload import receive_upload
from usb_diag_fat import FAT32ReadOnly

BUFFER = 0x20000000
SCRATCH = 0x24000000
BLOCK = 1024**2
FIP_SHA = '2e28a6d05bcda412195d4991beba403cc6fe6859d7770ebead91a34990c48fde'
FILES = ('boot.scr', 'Image', 'dtb/amlogic/meson-g12b-s922x-armhpc-oes1plus-v22.dtb', 'uInitrd.uboot')
ENV_KEYS = ('bootcmd','bootdelay','armhpc_mmc_boot','armhpc_usb_boot','autobootcmd','try_emmc_bootcmd',
            'upgrade_step','kernel_addr_r','fdt_addr_r','ramdisk_addr_r','fdt_high','stdin','stdout','stderr')
SOURCE_ITEMS = {
    'DDR.USB':'62eea16c67b0d16ba8310845a0edd3b2f71b1ef5019c577c2a5b94372f03cd7c',
    'DDR_ENC.USB':FIP_SHA,
    'UBOOT.USB':'62eea16c67b0d16ba8310845a0edd3b2f71b1ef5019c577c2a5b94372f03cd7c',
    'UBOOT_ENC.USB':FIP_SHA,
    'platform.conf':'1981261f893f6cf6c6c47ef5741b4d979ba926b126f238ca21e5ecd4c5467033',
}
RAM_STEPS = ('BurnStepCheckPassword','BurnStepBoardIsSecure','BurnStepDownloadSPL','BurnStepDownloadUboot')


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2); stream.write('\n')
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary,path)



def error_text(error):
    value=getattr(error,'strerror',None)
    if isinstance(value,bytes):
        try:return value.decode('utf-8')
        except UnicodeDecodeError:return value.decode('gb18030','replace')
    return str(error)


class CaptureSession:
    def __init__(self,directory,expected):
        need(not directory.exists(),'Choose a new diagnostic output directory');directory.mkdir(parents=True)
        self.directory=directory;self.path=directory/'report.json'
        self.data={'protocol':1,'operation':'OESP USB boot evidence','tool_version':'r4f',
            'session_id':uuid.uuid4().hex,'created':datetime.now(timezone.utc).isoformat(),
            'complete':False,'hardware_verified':False,'phase':'starting',
            'expected_release':expected['release'],'explicit_storage_write_commands':False,
            'vendor_mmc_initialization_side_effects_unverified':True,
            'ranges':{},'boot_files':{},'transport':{},'commands':[],'events':[]}
        save(self.path,self.data)
        print(json.dumps({'operation':'diagnostic session','session_id':self.data['session_id'],
                          'report':str(self.path)},ensure_ascii=False),flush=True)
    def emit(self,phase,**fields):
        self.data['phase']=phase
        event={'sequence':len(self.data['events'])+1,'time':datetime.now(timezone.utc).isoformat(),
               'monotonic_seconds':time.monotonic(),'phase':phase,**fields}
        self.data['events'].append(event);save(self.path,self.data)
        if phase not in ('command-submitted','command-completed'):
            print(json.dumps(event,ensure_ascii=False),flush=True)
    def fail(self,error):
        self.data['complete']=False
        self.data['error']={'message':error_text(error),'type':type(error).__name__,
                            'errno':getattr(error,'errno',None),'phase':self.data['phase']}
        self.emit('failed',message=error_text(error),error_type=type(error).__name__)


def is_administrator():
    if os.name!='nt':return None
    import ctypes
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def host_runtime():
    return {'platform':sys.platform,'python':sys.version.split()[0],
            'pointer_bits':struct.calcsize('P')*8,'administrator':is_administrator()}


def other_tool_check():
    if os.name!='nt':return
    result=subprocess.run(['tasklist','/FI','IMAGENAME eq USB_Burning_Tool.exe','/FO','CSV','/NH'],
                          capture_output=True,timeout=5)
    need(b'usb_burning_tool.exe' not in result.stdout.lower(),'Close USB Burning Tool before running the diagnostic')


def environment(block):
    valid = len(block)==65536 and struct.unpack_from('<I',block)[0]==zlib.crc32(block[4:])
    result = {'bytes':len(block),'sha256':sha(block),'crc4_valid':valid}
    if valid:
        values = dict(item.split('=',1) for item in block[4:].split(b'\0\0')[0].decode('utf-8','replace').split('\0') if '=' in item)
        result['boot_variables'] = {key:values[key] for key in ENV_KEYS if key in values}
    return result


def boot_area(data):
    payload = data[512:512+2097152]
    return {'bytes':len(data),'sha256':sha(data),'fip_sha256':sha(payload),
            'fip_matches_pinned':len(payload)==2097152 and sha(payload)==FIP_SHA,
            'fip_looks_erased':bool(payload) and (not any(payload) or all(x==255 for x in payload)),
            'info_first_32_hex':data[:32].hex()}


def allowed(command):
    if command in ('echo ARMHPC','mmc dev 1 0','mmc dev 1 1','mmc dev 1 2',
                   'mw.l 24000000 ${filesize} 1','mmc reg read extcsd 179 armhpc_diag_reg',
                   'mw.l 24000000 ${armhpc_diag_reg} 1'):
        return True
    match = re.fullmatch(r'mmc read 20000000 ([0-9a-f]+) ([0-9a-f]+)',command)
    if match:
        start,count = (int(x,16) for x in match.groups())
        key_start=(36*1024**2+16384)//512
        key_end=(36*1024**2+16384+256*1024)//512
        overlaps_key=start<key_end and start+count>key_start
        return 0<count<=BLOCK//512 and start+count<=9*1024**3//512 and not overlaps_key
    match=re.fullmatch(r'upload mem 0x([0-9a-f]+) normal 0x([0-9a-f]+)',command)
    if match:
        address,length=(int(value,16) for value in match.groups())
        return BUFFER<=address and 0<length<=BLOCK and address+length<=BUFFER+128*1024**2
    return command in {f'fatload mmc 1:1 20000000 {name}' for name in FILES}


class VendorCommandRejected(ValueError):
    def __init__(self,command,status):
        super().__init__('Vendor command rejected: '+command+'; '+status[:100])
        self.command=command
        self.status=status[:100]


class USBReader:
    def __init__(self,device,progress=None):
        self.device = device
        self.commands = []
        self.progress = progress
        self.input_pending = False
        self.fat_volume = None

    def command(self,command,timeout=15000):
        need(allowed(command),'Command is outside the diagnostic allowlist')
        self.commands.append(command)
        emit(self.progress,'command-submitted',command=command,wire_prefix_bytes=4)
        self.device.dev.ctrl_transfer(bmRequestType=0x40,bRequest=0x34,wValue=0,wIndex=2,
                   data_or_wLength='    '+command+'\0',timeout=timeout)
        deadline = time.monotonic()+timeout/1000
        while True:
            status = bytes(self.device.bulkCmdStat(timeout=timeout)).rstrip(b'\0')
            if status==b'success':
                emit(self.progress,'command-completed',command=command)
                return
            if status.startswith(b'failed:'):
                emit(self.progress,'command-rejected',command=command,status=status[:100].decode('utf-8','replace'))
                raise VendorCommandRejected(command,status[:100].decode('utf-8','replace'))
            need(status.startswith(b'Continue:34'),'Unexpected vendor command response: '+command)
            need(time.monotonic()<deadline,'Vendor command timed out: '+command)
            time.sleep(.1)

    def synchronise(self):
        command='echo ARMHPC'
        self.commands.append(command)
        emit(self.progress,'command-submitted',command=command,wire_prefix_bytes=4,channel='tpl-control')
        self.device.dev.ctrl_transfer(bmRequestType=0x40,bRequest=0x30,wValue=0,wIndex=1,
                   data_or_wLength='    '+command+'\0',timeout=5000)
        status=bytes(self.device.dev.ctrl_transfer(bmRequestType=0xc0,bRequest=0x31,wValue=0,wIndex=0,
                   data_or_wLength=64,timeout=5000)).rstrip(b'\0')
        need(status==b'success','TPL command synchronisation failed')
        emit(self.progress,'command-completed',command=command,channel='tpl-control')
        self.command(command,timeout=5000)

    def select(self,area,timeout=15000):
        need(area in (0,1,2),'Invalid diagnostic area')
        self.command('mmc dev 1 '+str(area),timeout=timeout)

    def ram(self,address,length):
        need(BUFFER<=address and address+length<=BUFFER+128*1024**2,'Invalid diagnostic RAM range')
        need(0<length<=BLOCK,'Invalid diagnostic transfer size')
        import usb.util
        interface = self.device.dev.get_active_configuration()[(0,0)]
        endpoint = usb.util.find_descriptor(interface,custom_match=lambda ep:
                    usb.util.endpoint_direction(ep.bEndpointAddress)==usb.util.ENDPOINT_IN)
        need(endpoint is not None,'USB input endpoint is missing')
        self.command(f'upload mem 0x{address:x} normal 0x{length:x}')
        emit(self.progress,'usb-read-started',address=hex(address),requested_bytes=length,
             expected_bytes=length,chunk_limit=4096,request='0x33',protocol='tpl-upload')
        self.input_pending = True
        def header():
            return self.device.dev.ctrl_transfer(bmRequestType=0xc0,bRequest=0x33,wValue=0,wIndex=0,
                                                data_or_wLength=16,timeout=5000)
        data=receive_upload(length,header,endpoint.read,progress=self.progress)
        self.input_pending = False
        return data

    def read(self,offset,length):
        need(offset>=0 and offset%512==0 and 0<length<=BLOCK and length%512==0,'Invalid diagnostic eMMC range')
        self.command(f'mmc read {BUFFER:x} {offset//512:x} {length//512:x}')
        return self.ram(BUFFER,length)

    def file(self,name,path):
        need(name in FILES,'Unexpected BOOT filename')
        if self.fat_volume is None:
            self.fat_volume=FAT32ReadOnly(self.read,100*1024**2,512*1024**2)
        last_bucket=[-1]
        def progress(copied,total):
            bucket=copied//(8*BLOCK)
            if bucket!=last_bucket[0] or copied==total:
                emit(self.progress,'boot-file-progress',file=name,bytes=copied,total=total,
                     size_source='fat32-directory')
                last_bucket[0]=bucket
        return self.fat_volume.copy_file(name,path,progress=progress)


class ConfigurationBackend:
    """libusb-win32 does not implement a configuration query; this loader uses 1."""
    def __init__(self,backend):
        self.backend=backend
    def get_configuration(self,handle):
        return 1
    def __getattr__(self,name):
        return getattr(self.backend,name)


def backend(driver_dll=None):
    import usb.backend.libusb0
    import usb.backend.libusb1
    if os.name=='nt':
        library = Path(driver_dll) if driver_dll else Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/libusb0.dll'
        need(library.is_file(),'libusb0.dll from the existing WorldCup driver is unavailable; no driver is installed by this program')
        value=usb.backend.libusb0.get_backend(find_library=lambda name:str(library.resolve()))
        label='existing WorldCup/libusb-win32 driver'
    else:
        value=usb.backend.libusb1.get_backend();label='libusb-1.0'
    need(value is not None,'USB backend could not be loaded')
    return ConfigurationBackend(value),label


def fresh_device(usb_backend):
    import usb.core
    from pyamlboot.pyamlboot import AmlogicSoC
    devices=list(usb.core.find(find_all=True,idVendor=0x1b8e,idProduct=0xc003,backend=usb_backend))
    need(devices,'No WorldCup device 1b8e:c003 is visible to the USB backend (count=0)')
    need(len(devices)==1,'Multiple WorldCup devices are visible (count='+str(len(devices))+'); connect only one OESP')
    device=AmlogicSoC.__new__(AmlogicSoC)
    device.dev=devices[0]
    device.dev.default_timeout=5000
    return device


def read_amlc_request(device):
    import usb.util
    interface=device.dev.get_active_configuration()[(0,0)]
    endpoint_in=usb.util.find_descriptor(interface,custom_match=lambda ep:
                usb.util.endpoint_direction(ep.bEndpointAddress)==usb.util.ENDPOINT_IN)
    endpoint_out=usb.util.find_descriptor(interface,custom_match=lambda ep:
                usb.util.endpoint_direction(ep.bEndpointAddress)==usb.util.ENDPOINT_OUT)
    need(endpoint_in is not None and endpoint_out is not None,'AMLC endpoints are missing')
    device.dev.ctrl_transfer(bmRequestType=0x40,bRequest=0x50,wValue=512,wIndex=0,
                            data_or_wLength=None,timeout=5000)
    packet=bytes(endpoint_in.read(512,5000))
    need(len(packet)>=16,'Short AMLC request')
    tag,length,offset=struct.unpack('<4s4xII',packet[:16])
    need(tag==b'AMLC','Unexpected AMLC request header: '+packet[:16].hex())
    endpoint_out.write(struct.pack('<4sIII',b'OKAY',0,0,0),5000)
    return length,offset


def connect(usb_backend,loader,load_ram,progress=None,attach_only=False):
    from pyamlboot.socid import SocId
    import pyamlboot.optimus as optimus
    device=fresh_device(usb_backend)
    soc=SocId(device.identify());before=str(soc)
    emit(progress,'device-identified',state=before)
    need(not soc.need_password or soc.password_ok,'The device requires a vendor USB password; no password bypass is attempted')
    if soc.stage_minor!=SocId.STAGE_MINOR_TPL:
        need(not attach_only,'Attach-only requires an existing TPL session; no RAM loading was performed')
        need((soc.stage_major,soc.stage_minor)==(0,SocId.STAGE_MINOR_IPL),
             'A previous SPL session is still present; re-enter USB download mode from a fresh power cycle before RAM loading')
        need(load_ram,'BootROM is present; --load-ram is required for the pinned volatile loader')
        from pyamlboot.amlimage import AmlImagePack
        from enum import Enum
        from types import SimpleNamespace
        loader=Path(loader)
        for name,digest in SOURCE_ITEMS.items():
            candidate=loader/name
            need(candidate.is_file() and not candidate.is_symlink() and sha(candidate.read_bytes())==digest,'Loader input differs: '+name)
        config='[LIST_NORMAL]\n'+''.join(f'file="{name}" main_type="USB" sub_type="{role}" file_type="normal"\n'
                    for name,role in (('DDR.USB','DDR'),('DDR_ENC.USB','DDR_ENC'),('UBOOT.USB','UBOOT'),('UBOOT_ENC.USB','UBOOT_ENC')))
        config+='file="platform.conf" main_type="conf" sub_type="platform" file_type="normal"\n[LIST_VERIFY]\n'
        need((loader/'image.cfg').read_text(encoding='ascii')==config,'Unexpected RAM-only loader configuration')
        optimus.USB_BACKEND=usb_backend
        with (loader/'image.cfg').open('rb') as stream:
            package=AmlImagePack(stream,True)
            args=SimpleNamespace(password=None,no_erase_bootloader=True,wipe=Enum('Wipe',{'no':0}).no,reset=False)
            # Missing optional platform values use the pinned upstream defaults.
            class DefaultsFilter(logging.Filter):
                def filter(self,record):return not record.getMessage().startswith('Platform parser: Config value')
            quiet=DefaultsFilter();logging.getLogger().addFilter(quiet)
            try:steps=optimus.get_burn_steps(args,optimus.SharedData(),package)
            finally:logging.getLogger().removeFilter(quiet)
            selected=[step for step in steps if type(step).__name__ in RAM_STEPS]
            need(tuple(type(step).__name__ for step in selected)==RAM_STEPS,'Unexpected upstream RAM loader sequence')
            for step in selected[:3]:
                emit(progress,'ram-step-started',step=type(step).__name__)
                step.do(device)
                emit(progress,'ram-step-completed',step=type(step).__name__)
            soc=SocId(device.identify())
            emit(progress,'spl-state',state=str(soc))
            if soc.stage_minor!=SocId.STAGE_MINOR_TPL:
                need((soc.stage_major,soc.stage_minor)==(1,8),'Unsupported OESP SPL state; RAM loading stopped')
                step=selected[3];step._dev=device;step._update_part()
                def reopen():return fresh_device(usb_backend)
                def inspect(candidate):
                    value=SocId(candidate.identify());return value.stage_major,value.stage_minor
                device=transfer_amlc(step._cur_img,lambda:read_amlc_request(device),
                    lambda seq,offset,data:device.writeAMLCData(seq,offset,data),
                    lambda:wait_for_tpl(reopen,inspect,lambda item:item.disposeDevice(),progress=progress),
                    lambda:device.disposeDevice(),progress=progress)
        soc=SocId(device.identify())
        need((soc.stage_major,soc.stage_minor)==(0,SocId.STAGE_MINOR_TPL),'RAM U-Boot did not enumerate as TPL')
    need((soc.stage_major,soc.stage_minor)==(0,SocId.STAGE_MINOR_TPL),'Device is not in a supported TPL state')
    emit(progress,'tpl-connected',state=str(soc))
    return USBReader(device,progress),{'before':before,'after':str(soc),'ram_loader_requested':load_ram,
                                     'attach_only':attach_only}


def capture(reader,directory,expected,stage,session=None,probe_only=False):
    if session is not None:
        report=session.data
        report['transport']=stage
        report_path=session.path
    else:
        return capture_new(reader,directory,expected,stage,probe_only=probe_only)
    return capture_into(reader,directory,expected,report,report_path,probe_only=probe_only)


def capture_new(reader,directory,expected,stage,probe_only=False):
    need(not directory.exists(),'Choose a new diagnostic output directory');directory.mkdir(parents=True)
    report={'protocol':1,'operation':'OESP USB boot evidence','created':datetime.now(timezone.utc).isoformat(),
            'complete':False,'hardware_verified':False,'transport':stage,'expected_release':expected['release'],
            'explicit_storage_write_commands':False,'vendor_mmc_initialization_side_effects_unverified':True,
            'ranges':{},'boot_files':{}}
    report_path=directory/'report.json';save(report_path,report)
    return capture_into(reader,directory,expected,report,report_path,probe_only=probe_only)


def capture_into(reader,directory,expected,report,report_path,probe_only=False):
    def area(name,offset,length):
        path=directory/(name+'.bin');digest=hashlib.sha256();first=bytearray()
        with path.open('xb') as target:
            for index in range(0,length,BLOCK):
                part=reader.read(offset+index,min(BLOCK,length-index));need(len(part)==min(BLOCK,length-index),'Short eMMC read')
                target.write(part);digest.update(part)
                if length<=3*BLOCK:first.extend(part)
                if index and index%(8*BLOCK)==0:
                    print(json.dumps({'operation':'read eMMC range','range':name,'bytes':index,'total':length}),flush=True)
            target.flush();os.fsync(target.fileno())
        row={'bytes':length,'sha256':digest.hexdigest(),'file':path.name}
        item=expected.get('ranges',{}).get(name)
        if item:row['matches_r4']=row['bytes']==item['bytes'] and row['sha256']==item['sha256']
        report['ranges'][name]=row;save(report_path,report);return bytes(first)
    report['collection_issues']=[]
    def issue(component,error,required=False):
        item={'component':component,'required':required,'type':type(error).__name__,'diagnosis':error_text(error)}
        report['collection_issues'].append(item)
        report['partial']=True
        save(report_path,report)
    try:
        reader.select(0)
        report['capture_scope']='probe' if probe_only else 'boot-evidence'
        report['transport']['ram_read_protocol']='tpl-upload-0x33'
        probe=reader.read(0,512);need(len(probe)==512,'Short initial sector probe')
        (directory/'user-probe.bin').write_bytes(probe)
        report['probe']={'bytes':len(probe),'sha256':sha(probe),'mbr_valid':probe[510:]==b'\x55\xaa',
                         'partition_starts':[struct.unpack_from('<I',probe,454+16*i)[0] for i in range(4)]}
        if expected.get('probe'):report['probe']['matches_r4']=sha(probe)==expected['probe']['sha256']
        save(report_path,report)
        if probe_only:
            report['complete']=True
            return report
        start=area('user-start',0,2097664);report['user_fip']=boot_area(start)
        mbr=start[:512];report['mbr']={'signature':hex(struct.unpack_from('<I',mbr,440)[0]),'valid':mbr[510:]==b'\x55\xaa',
              'partitions':[{'type':mbr[450+16*i], 'start':struct.unpack_from('<I',mbr,454+16*i)[0],
                             'sectors':struct.unpack_from('<I',mbr,458+16*i)[0]} for i in range(4)]}
        report['env_a']=environment(area('env-a',4194304,65536))
        report['env_b']=environment(area('env-b',4259840,65536))
        report['reserved_ept_coverage']={'offset':36*1024**2,'bytes_requested':4096,
            'scope':'partition-table-prefix','full_reserved_region_hash_available':False}
        try:
            data=area('ept-table',36*1024**2,4096)
            report['ept_table']={'available':True,'bytes':len(data),'magic_valid':data[:4]==b'MPT\0',
                                 'partition_count':struct.unpack_from('<I',data,16)[0],
                                 'matches_r4':report['ranges']['ept-table'].get('matches_r4')}
        except VendorCommandRejected as exc:
            report['ept_table']={'available':False,'diagnosis':error_text(exc)}
            issue('ept-table',exc)
        for index in (1,2):
            name='boot'+str(index-1)
            try:
                reader.select(index)
                report[name]=boot_area(area(name+'-start',0,2097664))
                report[name]['available']=True
            except VendorCommandRejected as exc:
                report[name]={'available':False,'diagnosis':error_text(exc)}
                issue(name,exc)
            finally:reader.select(0)
        boot_sector=area('boot-sector',100*1024**2,512)
        report['fat32_boot']={'label':boot_sector[71:82].decode('ascii','replace').strip(),
                             'signature':boot_sector[82:90].decode('ascii','replace')}
        try:
            reader.command('mmc reg read extcsd 179 armhpc_diag_reg');reader.command('mw.l 24000000 ${armhpc_diag_reg} 1')
            value=struct.unpack('<I',reader.ram(SCRATCH,4))[0]
            need(value<=255,'Invalid EXT_CSD byte')
            report['ext_csd_179']={'value':value,'boot_partition_enable':(value>>3)&7,'partition_access':value&7,'boot_ack':bool(value&64)}
        except (ValueError,AttributeError) as exc:
            if isinstance(exc,USBReadError) or getattr(reader,'input_pending',False):raise
            report['ext_csd_179']={'available':False,'diagnosis':error_text(exc)}
            issue('ext-csd-179',exc)
        need(report['mbr']['valid'] and report['mbr']['partitions'][0]['start']==204800 and
             report['fat32_boot']['signature']=='FAT32   ','Observed BOOT layout differs; file loading refused')
        for name in FILES:
            try:
                path=directory/('boot-'+Path(name).name)
                value=reader.file(name,path);reference=expected['boot_files'][name]
                value['matches_r4']=all(value[key]==reference[key] for key in ('bytes','sha256'))
                report['boot_files'][name]=value
                if not value['matches_r4']:
                    issue('boot-file:'+name,ValueError('BOOT file directory size or SHA256 differs from r4'),required=True)
            except Exception as exc:
                report['boot_files'][name]={'ok':False,'diagnosis':error_text(exc)}
                issue('boot-file:'+name,exc,required=True)
                if isinstance(exc,(OSError,USBReadError)) or getattr(reader,'input_pending',False):raise
            save(report_path,report)
        report['collection_finished']=True
        report['complete']=not any(row['required'] for row in report['collection_issues'])
        report['partial']=bool(report['collection_issues'])
        if not report['complete']:
            report['error']={'type':'BOOTFileMismatch','message':'Required BOOT file collection or SHA256 comparison failed',
                             'components':[row['component'] for row in report['collection_issues'] if row['required']]}
    except (Exception,KeyboardInterrupt) as exc:
        report['error']={'message':error_text(exc),'type':type(exc).__name__,
                         'phase':report.get('phase','capturing'),'errno':getattr(exc,'errno',None)}
        if isinstance(exc,USBReadError):
            report['error']['transfer']=exc.transfer
            if exc.__cause__ is not None:report['error']['usb_error']=error_text(exc.__cause__)
    finally:
        try:
            if isinstance(reader,USBReader):
                need(not reader.input_pending,'Incomplete USB input remains; re-enter USB download mode before another capture')
                reader.select(0,timeout=3000)
            else:reader.select(0)
            report['user_access_restored']=True
        except Exception as exc:report['user_access_restored']=False;report['restore_access_error']=error_text(exc)
        report['commands']=reader.commands;save(report_path,report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected',type=Path,default=Path(__file__).with_name('r4-expectations.json'))
    parser.add_argument('--output',type=Path,default=Path(__file__).with_name('private-reports'))
    parser.add_argument('--loader-directory',type=Path,default=Path(__file__).with_name('loader'))
    parser.add_argument('--driver-dll',type=Path)
    parser.add_argument('--load-ram',action='store_true')
    parser.add_argument('--attach-only',action='store_true')
    parser.add_argument('--check-runtime',action='store_true')
    parser.add_argument('--probe-only',action='store_true')
    args=parser.parse_args()
    need(not(args.attach_only and args.load_ram),'Choose load-ram or attach-only')
    if args.check_runtime:
        runtime,label=backend(args.driver_dll)
        print(json.dumps({'protocol':1,'ok':True,'backend':label,'connect_attempted':False}),flush=True);return
    expected=json.loads(args.expected.read_text(encoding='utf-8'))
    need(expected.get('board')=='wxy-oesp-v22' and expected.get('release')=='oesp-bookworm-1.1.0-20261013-06','Unexpected comparison input')
    directory=args.output/('capture-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6])
    session=CaptureSession(directory,expected)
    reader=None
    try:
        session.data['host']=host_runtime()
        session.emit('preflight',administrator=session.data['host']['administrator'])
        if os.name=='nt':
            need(session.data['host']['administrator'],
                 'Run RUN-DIAGNOSTICS.cmd as administrator to access the installed WorldCup driver')
        other_tool_check()
        runtime,label=backend(args.driver_dll)
        session.data['transport']['backend']=label
        session.emit('connecting')
        reader,stage=connect(runtime,args.loader_directory,args.load_ram,session.emit,args.attach_only)
        session.data['commands']=reader.commands
        session.emit('tpl-command-synchronisation')
        reader.synchronise()
        session.emit('capturing')
        report=capture(reader,directory,expected,{**stage,'backend':label},session=session,probe_only=args.probe_only)
        if not report['complete']:
            session.emit('capture-incomplete',error=report.get('error'))
        else:session.emit('capture-completed')
    except (Exception,KeyboardInterrupt) as exc:
        session.fail(exc)
    finally:
        if reader is not None:
            try:reader.device.disposeDevice()
            except Exception:pass
        save(session.path,session.data)
    print(json.dumps({'protocol':1,'ok':session.data['complete'],'report':str(session.path),
                     'session_id':session.data['session_id'],'complete':session.data['complete'],
                     'hardware_verified':False,'error':session.data.get('error')},ensure_ascii=False),flush=True)
    if not session.data['complete']:sys.exit(2)


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'protocol':1,'ok':False,'error':{'message':error_text(exc),'type':type(exc).__name__}},ensure_ascii=False),flush=True);sys.exit(2)
