# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Explicit, DATA-bound random-key encrypted swap profiles."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from config_io import (approve, atomic_write, configuration_lock, emit, entry, normal_system,
                       read_json, require, require_idle, run, safe_path, save_json, sync_directory)
from node_state import data_mount

PROFILE = 'node-config/armhpc/swap.json'
NAME = 'armhpc-swap'


def owned_mapping(value):
    record=read_json(Path('/run/armhpc/swap.json'),{})
    require(record.get('boot_id')==Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            and record.get('file')==value['file'] and record.get('loop','').startswith('/dev/loop'),
            'swap_mapping','The active mapping does not belong to this boot and profile')
    loop=record['loop']
    info=json.loads(run('losetup','--json','--list','--output','NAME,BACK-FILE',loop))['loopdevices']
    require(len(info)==1 and Path(info[0]['back-file']).samefile(value['file']),
            'swap_mapping','Loop backing file differs from the registered swap')
    mapping=run('cryptsetup','status',NAME)
    require(any(line.strip()=='device:  '+loop or
                line.strip().split()==['device:',loop] for line in mapping.splitlines()),
            'swap_mapping','Encrypted mapping uses another backing device')
    return loop


def stop():
    _,value=configuration()
    if not Path('/dev/mapper/'+NAME).exists():return status()
    loop=owned_mapping(value)
    run('swapoff','/dev/mapper/'+NAME,timeout=30)
    run('cryptsetup','close',NAME,timeout=10)
    run('losetup','-d',loop)
    save_json(Path('/run/armhpc/swap.json'),{'active':False,'stopped':True})
    return status()


def configuration():
    data, identity = data_mount()
    value = read_json(safe_path(data, PROFILE), {'schema_version': 1, 'profile': 'disabled', 'data': identity})
    require(value.get('schema_version') == 1 and value.get('profile') in ('disabled','host-noswap','limited'),
            'swap_schema', 'Unsupported swap profile')
    data_mount(expected=value['data'])
    return data, value


def status():
    try:
        _, value = configuration()
        swaps = [line.split() for line in Path('/proc/swaps').read_text().splitlines()[1:]]
        active = any(row and row[0] == '/dev/mapper/'+NAME for row in swaps)
        pod_limits={}
        for name in ('kubepods','kubepods.slice'):
            root=Path('/sys/fs/cgroup')/name
            if root.is_dir():
                for path in [root,*root.glob('*')]:
                    limit=path/'memory.swap.max'
                    if limit.is_file():pod_limits[str(path)]=limit.read_text().strip()
        return {'configured': value['profile'] != 'disabled', 'profile': value['profile'],
                'active': active, 'size_bytes': value.get('size_bytes', 0),
                'encrypted': active, 'key_persistence': 'none', 'hibernation_supported': False,
                'runtime': read_json(Path('/run/armhpc/swap.json'), {}),'pod_cgroup_swap_limits':pod_limits,
                'pod_swap_gate':('blocked_by_observed_ancestor' if any(v=='0' and Path(p).name in ('kubepods','kubepods.slice') for p,v in pod_limits.items()) else
                                 'workload_confirmation_required' if pod_limits else 'no_pod_cgroup_observed')}
    except (RuntimeError, OSError, ValueError) as error:
        return {'configured': False, 'active': False, 'error': str(error)}


def plan(profile, size_gib=16, allow_degraded=False, import_file=None):
    data, identity = data_mount()
    require(profile in ('disabled','host-noswap','limited'), 'swap_profile', 'Unknown swap profile')
    require(isinstance(size_gib, int) and 1 <= size_gib <= 64, 'swap_size', 'Select 1–64 GiB')
    file = Path(import_file) if import_file else data / '.armhpc/swapfile'
    require(file.is_absolute() and file.is_relative_to(data) and file != data,
            'swap_file', 'Swap backing file must be inside the verified DATA mount')
    safe_path(data, str(file.relative_to(data)))
    value = {'schema_version':1, 'profile':profile, 'size_bytes':size_gib*1024**3,
             'file':str(file), 'data':identity, 'allow_degraded_noswap':bool(allow_degraded),
             'import_existing':bool(import_file), 'key_persistence':'none'}
    if profile != 'disabled':
        import shutil
        require(file.exists() or shutil.disk_usage(data).free >= value['size_bytes'] + 2*1024**3,
                'swap_space', 'Insufficient DATA space, including a 2 GiB operating reserve')
        old = read_json(safe_path(data, PROFILE), {})
        if file.exists():
            require(import_file or old.get('file') == str(file), 'swap_owned', 'Existing backing file is not registered; explicitly import a known swap file')
            validate_file(file, value['size_bytes'])
    return value


def validate_file(file, size, inode=None):
    require(not file.is_symlink() and file.is_file(), 'swap_file', 'Swap backing file must be a regular file')
    info = file.stat()
    require(info.st_uid == 0 and info.st_mode & 0o777 == 0o600 and info.st_size == size
            and info.st_blocks * 512 >= info.st_size, 'swap_file', 'Swap file must be fully allocated, root-owned and mode 0600')
    if inode:
        require(info.st_ino == inode, 'swap_identity', 'Swap backing file was replaced')


def apply(value):
    from resource_config import version_supported,validate_kubelet_profile
    from kube_runtime import refresh_recovery,verify_recovery
    verify_recovery()
    version_supported()
    validate_kubelet_profile(('failSwapOn','memorySwap'),'90-armhpc-swap.conf')
    if value['profile']!='disabled':
        from node_state import kernel_status
        require(kernel_status()['features']['encrypted_swap']['ready'],'swap_kernel',
                'Encrypted swap dependencies are missing from the running kernel')
    data, _ = data_mount(expected=value['data'])
    require(not status()['active'],'swap_active','Disable the active owned swap mapping before changing its profile')
    for unit in ('k3s.service','k3s-agent.service'):
        active = subprocess.run(['systemctl','is-active','--quiet',unit], timeout=5).returncode == 0
        require(not active, 'swap_service_active', 'Stop/drain the affected K3s node before changing its swap profile')
    file = Path(value['file'])
    if value['profile'] != 'disabled':
        if not file.exists():
            file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.posix_fallocate(fd, 0, value['size_bytes'])
                os.fsync(fd)
            finally:
                os.close(fd)
            sync_directory(file.parent)
        validate_file(file, value['size_bytes'])
        value['inode'] = file.stat().st_ino
    path = safe_path(data, PROFILE)
    if path.exists():
        atomic_write(path.with_name('swap.previous.json'), path.read_bytes(), 0o600)
    save_json(path, value)
    write_kubelet(data,value)
    for unit in ('k3s.service','k3s-agent.service'):
        atomic_write(safe_path(Path('/'), 'etc/systemd/system/'+unit+'.d/30-armhpc-swap.conf'),
                     '[Unit]\nRequires=armhpc-swap.service\nAfter=armhpc-swap.service\n\n[Service]\nMemorySwapMax=0\n')
    run('systemctl','daemon-reload')
    refresh_recovery(changed=['data/k3s/agent/etc/kubelet.conf.d/90-armhpc-swap.conf',
        'etc/systemd/system/k3s.service.d/30-armhpc-swap.conf','etc/systemd/system/k3s-agent.service.d/30-armhpc-swap.conf'])
    return {'configuration':value, 'activation':'next service start', 'data_modified':value['profile']!='disabled'}


def write_kubelet(data,value,degraded=False):
    dropin={'apiVersion':'kubelet.config.k8s.io/v1beta1','kind':'KubeletConfiguration',
            'failSwapOn':degraded or value['profile']=='disabled',
            'memorySwap':{'swapBehavior':'LimitedSwap' if value['profile']=='limited' and not degraded else 'NoSwap'}}
    destination=safe_path(data,'k3s/agent/etc/kubelet.conf.d/90-armhpc-swap.conf')
    changed=not destination.is_file() or json.loads(destination.read_text())!=dropin
    if changed:atomic_write(destination,json.dumps(dropin,indent=2)+'\n',0o600)
    return changed


def start():
    data, value = configuration()
    if value['profile'] == 'disabled':
        return status()
    file = Path(value['file'])
    safe_path(data, str(file.relative_to(data)))
    validate_file(file, value['size_bytes'], value['inode'])
    if status()['active']:
        owned_mapping(value)
        return status()
    require(not Path('/dev/mapper/'+NAME).exists(), 'swap_mapping', 'Unowned/stale mapping exists; inspect it before retrying')
    loop = run('losetup','--find','--show',file)
    try:
        run('cryptsetup','open','--type','plain','--cipher','aes-xts-plain64','--key-size','256',
            '--key-file','/dev/urandom',loop,NAME,timeout=15)
        run('mkswap','/dev/mapper/'+NAME)
        run('swapon','/dev/mapper/'+NAME)
        save_json(Path('/run/armhpc/swap.json'), {'loop':loop,'file':str(file),'encrypted':True,'active':True,
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()})
        if write_kubelet(data,value):
            from kube_runtime import refresh_recovery
            refresh_recovery(changed=['data/k3s/agent/etc/kubelet.conf.d/90-armhpc-swap.conf'],allow_uninstalled=True)
    except Exception as error:
        subprocess.run(['cryptsetup','close',NAME],capture_output=True,timeout=5)
        subprocess.run(['losetup','-d',loop],capture_output=True,timeout=5)
        save_json(Path('/run/armhpc/swap.json'), {'active':False,'error':str(error),'degraded':value['allow_degraded_noswap']})
        if not value['allow_degraded_noswap']:
            raise
        if write_kubelet(data,value,True):
            from kube_runtime import refresh_recovery
            refresh_recovery(changed=['data/k3s/agent/etc/kubelet.conf.d/90-armhpc-swap.conf'],allow_uninstalled=True)
    return status()


def main():
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG','armhpc swap'))
    parser.add_argument('action', nargs='?', default='status', choices=('status','set','stop','_start','_stop'))
    parser.add_argument('--profile',choices=('disabled','host-noswap','limited'),default='disabled')
    parser.add_argument('--size-gib',type=int,default=16)
    parser.add_argument('--import-existing',type=Path)
    parser.add_argument('--allow-degraded-noswap',action='store_true')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--yes',action='store_true')
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args()
    normal_system(Path('/'))
    if args.action=='status':result=status()
    elif args.action=='_start':result=start()
    elif args.action=='_stop':result=stop()
    else:
        with configuration_lock(Path('/')):
            require_idle(Path('/'))
            if args.action=='stop':
                approve(args.yes,'Disable this boot\'s registered encrypted swap mapping?')
                for unit in ('k3s.service','k3s-agent.service'):
                    require(subprocess.run(['systemctl','is-active','--quiet',unit],timeout=5).returncode!=0,
                            'swap_service_active','Stop the drained K3s service before disabling swap')
                emit(stop(),args.json);return
            value=plan(args.profile,args.size_gib,args.allow_degraded_noswap,args.import_existing)
            if args.apply:
                approve(args.yes,'Apply this DATA-bound encrypted swap profile?')
                result=apply(value)
            else:result={'plan':value,'applied':False}
    emit(result,args.json)


if __name__=='__main__':
    raise SystemExit(entry(main))
