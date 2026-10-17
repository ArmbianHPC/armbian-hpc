#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Load a task-bound, signed Rescue pair into volatile RAM through WorldCup."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parent))
from armhpc_ota.boot_platform import arguments,ramdisk,validate_load_window
from armhpc_ota.io import sha256,atomic_json
from armhpc_ota.metadata import verify,parse_json,valid_sha
from armhpc_ota.config import Config
from usb_diag_fat import FAT32ReadOnly
from usb_diag_upload import receive_upload

ROOT=Path(__file__).resolve().parent
ADDRESSES={'kernel':0x08080000,'dtb':0x08008000,'initramfs':0x13000000}


def need(condition,message):
    if not condition:raise ValueError(message)


def prepare(export,trust,directory):
    export=Path(export)
    need(export.name=='BOOT-export.json' and not export.is_symlink() and export.stat().st_size<=1024**2,
         'Select the original private BOOT-export.json')
    data=parse_json(export.read_bytes())
    need(data.get('protocol')==1 and data.get('kind')=='private-boot-export','Unsupported BOOT export')
    need(str(uuid.UUID(data['task_id']))==data['task_id'],'Invalid original task ID')
    valid_sha(data['device_id']);valid_sha(data['emmc_cid_sha256'])
    recovery=data['recovery'];board=Config(profile='wxy-oesp-v22').board()
    need(recovery['board']==board['id'] and recovery['layout']==board['layout'] and
         recovery['anchor_abi']==board['anchor_abi'] and recovery['slot'] in ('a','b'),'Recovery board/layout differs')
    signed=verify(json.dumps(recovery['signed_manifest']).encode(),Path(trust))
    need(signed.get('kind')=='boot-maintenance' and signed['board']==board['id'] and signed['architecture']=='arm64' and
         signed['layout']==board['layout'] and signed['anchor_abi']==board['anchor_abi'],'Signed BOOT pairing differs')
    need(data['boot']['start_lba']==board['boot_start'] and data['boot']['sectors']==board['boot_sectors'],
         'Export BOOT geometry differs')
    files=recovery['files'];image=export.parent/'BOOT-rescue.img';item=data['files']['BOOT-rescue.img']
    need(image.is_file() and not image.is_symlink() and image.stat().st_size==item['bytes']==board['boot_sectors']*512 and
         sha256(image)==item['sha256'],'Private BOOT recovery image size/hash differs')
    from armhpc_ota.boot_maintenance import select_recovery_initramfs
    recovery_name=select_recovery_initramfs(signed,files)
    need(signed['files'][recovery_name]['sha256']==files['initramfs']['sha256'],'Signed recovery hashes differ')
    paths={}
    with image.open('rb') as source:
        def read_at(offset,length):source.seek(offset);return source.read(length)
        volume=FAT32ReadOnly(read_at,0,image.stat().st_size)
        for name in ADDRESSES:
            target=Path(directory)/name;paths[name]=target
            volume.copy_file('armhpc-recovery/'+name,target)
            need(target.stat().st_size==files[name]['bytes'] and sha256(target)==files[name]['sha256'],
                 'Extracted recovery file differs: '+name)
    validate_load_window(board,paths['kernel'],paths['dtb'],paths['initramfs'])
    # The signed initramfs must support a missing or unmountable BOOT filesystem.
    # The device task can also use a same-pair earlier initramfs when BOOT remains intact.
    return data,paths,board


class RAMChannel:
    def __init__(self,device,regions,progress):
        self.device=device;self.regions=regions;self.progress=progress

    def contains(self,address,length):
        return length>0 and any(start<=address and address+length<=start+size for start,size in self.regions)

    def command(self,command,handoff=False):
        need(len(('    '+command+'\0').encode('ascii'))<128,'RAM command exceeds the vendor limit')
        allowed=command in ('setenv bootargs ""','setenv fdt_high 0x20000000','fdt addr 0x08008000','booti 0x08080000 0x13000000 0x08008000')
        match=re.fullmatch(r'(?:download|upload) mem 0x([0-9a-f]+) normal 0x([0-9a-f]+)',command)
        if match:allowed=self.contains(*[int(v,16) for v in match.groups()])
        if re.fullmatch(r'setenv bootargs "[a-zA-Z0-9=,. /_$:{}+-]+"',command):allowed=True
        need(allowed,'Command is outside the volatile recovery allowlist')
        self.progress('ram-command',command=command)
        self.device.dev.ctrl_transfer(bmRequestType=0x40,bRequest=0x34,wValue=0,wIndex=2,
                                     data_or_wLength='    '+command+'\0',timeout=5000)
        if handoff:return
        deadline=time.monotonic()+15
        while True:
            status=bytes(self.device.bulkCmdStat(timeout=5000)).rstrip(b'\0')
            if status==b'success':return
            need(status.startswith(b'Continue:34'),'Vendor RAM command was rejected: '+status[:100].decode('ascii','replace'))
            need(time.monotonic()<deadline,'Vendor RAM command timeout')

    def read(self,address,length):
        import usb.util
        need(length<=1024**2 and self.contains(address,length),'RAM read is outside a validated payload')
        self.command(f'upload mem 0x{address:x} normal 0x{length:x}')
        interface=self.device.dev.get_active_configuration()[(0,0)]
        endpoint=usb.util.find_descriptor(interface,custom_match=lambda ep:
            usb.util.endpoint_direction(ep.bEndpointAddress)==usb.util.ENDPOINT_IN)
        need(endpoint is not None,'Missing USB input endpoint')
        def header():return self.device.dev.ctrl_transfer(bmRequestType=0xc0,bRequest=0x33,wValue=0,wIndex=0,
                                                          data_or_wLength=16,timeout=5000)
        return receive_upload(length,header,endpoint.read)

    def write(self,address,data,name):
        need(self.contains(address,len(data)),'RAM payload exceeds validated bounds')
        self.command(f'download mem 0x{address:x} normal 0x{len(data):x}')
        for sequence,offset in enumerate(range(0,len(data),65536)):
            chunk=data[offset:offset+65536]
            need(self.device.writeMedia(chunk,seq=sequence,retryTimes=0),'Short USB RAM write')
            deadline=time.monotonic()+15
            while True:
                ack=bytes(self.device.devRead(512,timeout=5000))
                if ack.startswith(b'OK!!'):break
                need(ack.startswith(b'Continue:32') and time.monotonic()<deadline,'USB RAM write acknowledgement failed')
            if offset%(8*1024**2)==0 or offset+len(chunk)==len(data):
                self.progress('ram-upload',file=name,bytes=offset+len(chunk),total=len(data))
        digest=hashlib.sha256()
        for offset in range(0,len(data),1024**2):
            digest.update(self.read(address+offset,min(1024**2,len(data)-offset)))
        need(digest.digest()==hashlib.sha256(data).digest(),'RAM readback hash differs: '+name)
        self.progress('ram-verified',file=name,bytes=len(data),sha256=digest.hexdigest())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('prepare','boot'))
    parser.add_argument('--export',type=Path,required=True)
    parser.add_argument('--trust',type=Path,default=ROOT/'trust')
    parser.add_argument('--loader',type=Path,default=ROOT/'loader')
    parser.add_argument('--report',type=Path)
    parser.add_argument('--yes',action='store_true')
    parser.add_argument('--load-ram',action='store_true')
    args=parser.parse_args()
    report=args.report or args.export.parent/('USB-rescue-'+uuid.uuid4().hex+'.json')
    state={'protocol':1,'operation':'volatile-rescue','hardware_verified':False,'storage_write_commands':False,
           'stage':'validating','events':[]}
    def progress(phase,**details):
        state['events'].append({'sequence':len(state['events'])+1,'phase':phase,**details})
        state['stage']=phase;atomic_json(report,state);print(json.dumps(state['events'][-1]),flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix='armhpc-usb-rescue-') as directory:
            data,paths,board=prepare(args.export,args.trust,directory)
            state.update(task_id=data['task_id'],device_id=data['device_id'],slot=data['recovery']['slot'])
            progress('validated')
            if args.operation=='prepare':print(json.dumps({'protocol':1,'ok':True,'data':state}));return 0
            need(args.yes,'Use --yes to start the displayed volatile Rescue recovery')
            if os.name=='nt':
                import ctypes
                need(ctypes.windll.shell32.IsUserAnAdmin(),'Run as administrator with USB Burning Tool closed')
            spec=importlib.util.spec_from_file_location('armhpc_usb_diagnose',ROOT/'oesp-usb-diagnose.py')
            diagnostics=importlib.util.module_from_spec(spec);spec.loader.exec_module(diagnostics)
            backend,_=diagnostics.backend(None)
            reader,transport=diagnostics.connect(backend,args.loader,args.load_ram,progress)
            state['transport']=transport;reader.synchronise()
            payloads={name:path.read_bytes() for name,path in paths.items()}
            payloads['initramfs']=ramdisk(payloads['initramfs'])
            channel=RAMChannel(reader.device,[(ADDRESSES[name],len(payload)) for name,payload in payloads.items()],progress)
            for name,payload in payloads.items():channel.write(ADDRESSES[name],payload,name)
            channel.command('setenv fdt_high 0x20000000')
            channel.command('setenv bootargs ""')
            for part in arguments(board,data['recovery']['slot'],recovery=True).split():
                channel.command('setenv bootargs "${bootargs} '+part+'"')
            channel.command('fdt addr 0x08008000')
            progress('handoff-submitted',next_action='Reconnect by the exported device ID; query original BOOT task')
            try:channel.command('booti 0x08080000 0x13000000 0x08008000',handoff=True)
            except Exception as error:state['handoff_error']=str(error)
            # Submission and a disappearing USB device are not boot confirmation.
            state['stage']='awaiting-linux-confirmation';atomic_json(report,state)
            print(json.dumps({'protocol':1,'ok':True,'data':state}));return 0
    except Exception as error:
        progress('failed',error_type=type(error).__name__,message=str(error))
        print(json.dumps({'protocol':1,'ok':False,'error':{'message':str(error)},'report':str(report)}));return 2


if __name__=='__main__':raise SystemExit(main())
