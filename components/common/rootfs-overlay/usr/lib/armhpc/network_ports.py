#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Stable onboard port names and identities selected by a board policy."""
import hashlib, json, os, re, subprocess, sys, time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from config_io import save_json

def valid_mac(value):
    return bool(re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',value) and value!='00:00:00:00:00:00'
                and not int(value[:2],16)&1)

def driver(path):
    return (path/'device/driver').resolve().name

def ports(net=Path('/sys/class/net')):
    found={}
    for target in ('eth0','eth1'):
        candidates=[]
        for p in net.iterdir():
            if not (p/'device').exists(): continue
            name=driver(p)
            if target=='eth0' and ('meson' in name or 'stmmac' in name): candidates.append(p)
            if target=='eth1' and name=='r8152': candidates.append(p)
        if len(candidates)>1: raise ValueError('Ambiguous onboard port '+target)
        if candidates: found[target]=candidates[0]
    return found

def configure(store):
    deadline=time.monotonic()+15
    while True:
        selected=ports()
        if len(selected)==2 or time.monotonic()>=deadline: break
        time.sleep(.25)
    if not selected: raise ValueError('No OESP onboard network port is available')
    cids=[p.read_text().strip() for p in Path('/sys/bus/mmc/devices').glob('*/cid')
          if (p.parent/'type').read_text().strip()=='MMC']
    if len(cids)!=1: raise ValueError('Expected one eMMC CID for port identity')
    owner=hashlib.sha256(cids[0].encode()).hexdigest()
    path=Path(store)/'var/lib/armhpc/network-ports-identity.json'
    saved=json.loads(path.read_text()) if path.is_file() else {'protocol':1,'cid_sha256':owner,'ports':{}}
    if saved.get('cid_sha256')!=owner: raise ValueError('Network identity belongs to another eMMC')
    receipts={}
    for target,p in selected.items():
        previous=saved['ports'].get(target,{})
        mac=(p/'address').read_text().strip().lower()
        if previous:
            if not valid_mac(previous['mac']): raise ValueError('Invalid saved port MAC')
            mac=previous['mac']; origin=previous['origin']
        elif valid_mac(mac): origin='device-tree-or-controller'
        else:
            raw=bytearray(hashlib.sha256((owner+':'+target).encode()).digest()[:6]);raw[0]=(raw[0]&254)|2
            mac=':'.join(format(x,'02x') for x in raw);origin='persisted-cid-derived'
        receipts[target]={'mac':mac,'origin':origin,'device_path':str((p/'device').resolve()),'driver':driver(p)}
    if len({r['mac'] for r in receipts.values()})!=len(receipts): raise ValueError('Onboard ports have duplicate MAC addresses')
    for target,p in selected.items():
        subprocess.run(['ip','link','set','dev',p.name,'down'],check=True)
    # Free both names before assigning them, including a reversed kernel enumeration.
    pending={}
    for index,(target,p) in enumerate(selected.items()):
        temporary='ahpcport'+str(index)
        if Path('/sys/class/net',temporary).exists(): raise ValueError('Temporary port name is occupied')
        subprocess.run(['ip','link','set','dev',p.name,'name',temporary],check=True);pending[target]=temporary
    for target,name in pending.items():
        subprocess.run(['ip','link','set','dev',name,'name',target],check=True)
        subprocess.run(['ip','link','set','dev',target,'address',receipts[target]['mac']],check=True)
    saved.update(ports=receipts);save_json(path,saved)
    save_json(Path('/run/armhpc/network-ports.json'),saved)
    primary='eth0' if 'eth0' in receipts else 'eth1'
    net0=receipts[primary]; receipt={'schema_version':1,'interface':primary,'mac':net0['mac'],
                                 'origin':net0['origin'],'source':'amlogic-board-ports','emmc_cid':cids[0]}
    save_json(Path(store)/'var/lib/armhpc/network-identity.json',receipt)
    save_json(Path('/run/armhpc/network-identity.json'),receipt)
    return net0['mac']

def report(net=Path('/sys/class/net')):
    result=[]
    for target,p in ports(net).items():
        def read(name):
            try:return (p/name).read_text().strip()
            except OSError:return None
        usb_speed=None
        if target=='eth1':
            for parent in ((p/'device').resolve(),*(p/'device').resolve().parents):
                if (parent/'speed').is_file() and (parent/'idVendor').is_file():
                    usb_speed=(parent/'speed').read_text().strip();break
        result.append({'interface':p.name,'physical_port':'NET0' if target=='eth0' else 'NET1',
                       'driver':driver(p),'carrier':read('carrier'),'link_mbps':read('speed'),
                       'usb_bus_mbps':usb_speed,'identify_supported':target=='eth0'})
    return result
