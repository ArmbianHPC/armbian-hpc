# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Explicit kubelet reservations based on measured memory and selected node role."""
import argparse
import json
from pathlib import Path
import re
import subprocess
from config_io import (approve,atomic_write,configuration_lock,emit,entry,normal_system,
                       read_json,require,require_idle,run,safe_path,save_json)
from node_state import data_mount

PROFILE='node-config/armhpc/resources.json'
DROPIN='k3s/agent/etc/kubelet.conf.d/85-armhpc-resources.conf'


def version_supported():
    binary=Path('/usr/local/bin/k3s')
    require(binary.is_file(),'k3s_missing','Install the selected K3s version before enabling kubelet profiles')
    text=run(binary,'--version')
    match=re.search(r'\bv(\d+)\.(\d+)\.(\d+)\+k3s\d+',text)
    require(match and tuple(map(int,match.groups()))>=(1,32,0),'kubelet_dropin',
            'Persistent kubelet configuration directories require K3s v1.32 or newer; retain the existing version and review its adapter')
    return match[0]


def plan(role,tier=None):
    require(role in ('server','worker'),'resource_role','Select server or worker')
    memory=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:')))*1024
    detected=2 if memory<=int(2.5*1024**3) else 4
    tier=tier or detected
    require(tier in (2,4) and tier<=detected,'resource_memory','The selected tier exceeds measured MemTotal')
    system=192 if tier==2 else 256
    kube=256 if role=='server' else 192
    eviction=200 if tier==2 else 400
    require((system+kube+eviction)*1024**2<memory//2,'resource_budget','Reservations leave insufficient measured memory')
    data,identity=data_mount()
    current=read_json(safe_path(data,PROFILE),{})
    configuration={'apiVersion':'kubelet.config.k8s.io/v1beta1','kind':'KubeletConfiguration',
                   'systemReserved':{'memory':str(system)+'Mi','cpu':'100m'},
                   'kubeReserved':{'memory':str(kube)+'Mi','cpu':'100m'},
                   'evictionHard':{'memory.available':str(eviction)+'Mi','nodefs.available':'10%',
                     'imagefs.available':'15%','nodefs.inodesFree':'5%','imagefs.inodesFree':'5%'},
                   'mergeDefaultEvictionSettings':True}
    return {'schema_version':1,'role':role,'tier_gib':tier,'memtotal_bytes':memory,'data':identity,
            'estimated_allocatable_bytes':memory-(system+kube+eviction)*1024**2,'eviction_memory_bytes':eviction*1024**2,
            'configuration':configuration,'previous':current,'activation':'restart the drained K3s service after review'}


def validate_kubelet_profile(keys,owned):
    import yaml
    policy=read_json(Path('/etc/image-framework/k3s.json'),{})
    config=Path(policy.get('configuration_dir','/data/node-config/k3s'))/'config.yaml'
    value=yaml.safe_load(config.read_text()) if config.is_file() else {}
    names={re.sub(r'(?<!^)(?=[A-Z])','-',name).lower() for name in keys}
    require(not any(str(arg).split('=',1)[0] in names for arg in (value or {}).get('kubelet-arg',[])),
            'kubelet_profile_conflict','Existing kubelet arguments override the requested profile')
    for path in Path('/data/k3s/agent/etc/kubelet.conf.d').glob('*.conf'):
        if path.name==owned:continue
        existing=yaml.safe_load(path.read_text())
        require(isinstance(existing,dict) and not (set(existing)&set(keys)),
                'kubelet_profile_conflict','Another kubelet configuration owns these settings: '+path.name)


def apply(value):
    version_supported()
    from kube_runtime import refresh_recovery,verify_recovery
    verify_recovery()
    data,_=data_mount(expected=value['data'])
    for unit in ('k3s.service','k3s-agent.service'):
        require(subprocess.run(['systemctl','is-active','--quiet',unit],timeout=5).returncode!=0,
                'resource_service_active','Drain and stop K3s before changing reservations')
    validate_kubelet_profile(('systemReserved','kubeReserved','evictionHard'),Path(DROPIN).name)
    path=safe_path(data,PROFILE)
    if path.exists():atomic_write(path.with_name('resources.previous.json'),path.read_bytes(),0o600)
    destination=safe_path(data,DROPIN)
    previous=value.get('previous',{})
    if destination.exists():
        require(previous.get('configuration')==json.loads(destination.read_text()),
                'resource_conflict','The managed kubelet file was changed outside this profile')
    atomic_write(destination,json.dumps(value['configuration'],indent=2)+'\n',0o600)
    value={k:v for k,v in value.items() if k!='previous'}
    save_json(path,value)
    refresh_recovery(changed=['data/'+DROPIN])
    return value


def status():
    data,identity=data_mount()
    value=read_json(safe_path(data,PROFILE),None)
    return {'profile':value,'configured':bool(value),'defaults_inherited':not bool(value),'data':identity,
            'kubelet_files':[p.name for p in (data/'k3s/agent/etc/kubelet.conf.d').glob('*.conf')]}


def main():
    parser=argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG','armhpc resources'))
    parser.add_argument('action',nargs='?',default='status',choices=('status','set'))
    parser.add_argument('--role',choices=('server','worker'))
    parser.add_argument('--memory-gib',type=int,choices=(2,4))
    parser.add_argument('--apply',action='store_true');parser.add_argument('--yes',action='store_true');parser.add_argument('--json',action='store_true')
    args=parser.parse_args();normal_system(Path('/'))
    if args.action=='status':result=status()
    else:
        require(args.role,'resource_role','Select --role server or worker')
        with configuration_lock(Path('/')):
            require_idle(Path('/'));result=plan(args.role,args.memory_gib)
            if args.apply:
                approve(args.yes,'Apply the measured-memory kubelet reservations?');result=apply(result)
    emit(result,args.json)


if __name__=='__main__':raise SystemExit(entry(main))
