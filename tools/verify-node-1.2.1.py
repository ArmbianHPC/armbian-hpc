#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Collect local firmware checks and compare an explicitly saved DATA baseline."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def command(*arguments,timeout=20):
    result=subprocess.run(list(map(str,arguments)),text=True,capture_output=True,timeout=timeout)
    if result.returncode:raise RuntimeError(' '.join(map(str,arguments))+': '+result.stderr.strip()[-300:])
    return result.stdout.strip()


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    actions=parser.add_mutually_exclusive_group()
    actions.add_argument('--baseline',action='store_true',help='Save a baseline and a 1 MiB marker on verified DATA')
    actions.add_argument('--compare',action='store_true',help='Compare the saved system baseline after an upgrade')
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('Run as root')
    sys.path.insert(0,'/usr/lib/armhpc-ota');sys.path.insert(0,'/usr/lib/armhpc')
    from armhpc_ota.config import Config
    from armhpc_ota.io import atomic_json,fsync_dir,writer_lock
    from node_state import kernel_status,data_mount,status as state_status
    from network_ready import inspect,policy
    c=Config.load()
    cli='armhpc-rescue' if Path('/etc/armhpc/rescue-environment').is_file() else 'armhpc'
    s=json.loads(command(cli,'ota','status','--json'))['data']
    mount=json.loads(command('findmnt','-J','-M',c.state,'-o','TARGET,FSTYPE,LABEL,OPTIONS'))['filesystems']
    if len(mount)!=1 or mount[0]['fstype']!='ext4' or mount[0]['label']!='OTA_SHARED' or 'rw' not in mount[0]['options'].split(','):
        raise RuntimeError('A real writable OTA_SHARED ext4 mount is required to store a report')
    checks=[]
    def check(name,value,scope='local'):
        checks.append({'check':name,'passed':bool(value),'scope':scope})
    check('agent 1.2.1',s.get('agent_version')=='1.2.1')
    kernel=kernel_status();check('final kernel capabilities and modules',kernel['ready'])
    management=inspect(policy());check('management carrier, address and route',management['ready'])
    check('clock anchor',s.get('clock',{}).get('quality') in ('estimated','synchronised'))
    check('health timer',command('systemctl','is-active','armhpc-health.timer')=='active')
    failed=command('systemctl','--failed','--no-legend','--plain','--no-pager')
    check('no failed services',not failed)
    folder=c.state/'operator-validation';folder.mkdir(parents=True,mode=0o700,exist_ok=True)
    baseline=folder/'system-1.3-before.json'
    comparison=None
    if s.get('mode')=='system':
        data,identity=data_mount();check('DATA identity and writable ext4',True)
        root_source=command('findmnt','-n','-o','SOURCE','--mountpoint','/')
        check('64 GiB SATA system partition',int(command('blockdev','--getsize64',root_source))==64*1024**3)
        state=state_status();check('registered program/configuration resources',state['installed']['ready'])
        marker=data/'.armhpc-ota-validation-marker'
        def current():
            return {'device_id':s.get('device_id'),'hostname':Path('/etc/hostname').read_text().strip(),
                    'machine_id':Path('/etc/machine-id').read_text().strip(),'data':identity,
                    'ssh_public_keys':{p.name:digest(p) for p in sorted(Path('/etc/ssh').glob('ssh_host_*_key.pub'))},
                    'network':{p.name:digest(p) for p in sorted(Path('/etc/systemd/network').glob('*')) if p.is_file()},
                    'authentication':{str(p):digest(p) for p in (Path('/etc/shadow'),Path('/root/.ssh/authorized_keys'),
                                                               Path('/etc/ssh/image-framework-admin.conf')) if p.is_file()},
                    'mac':{p.name:(p/'address').read_text().strip() for p in Path('/sys/class/net').glob('eth*')},
                    'marker_sha256':digest(marker) if marker.is_file() and not marker.is_symlink() else None}
        with writer_lock(folder/'system-1.3.lock'):
            if args.baseline:
                if baseline.exists():raise RuntimeError('A baseline already exists; retain it and use --compare')
                if s.get('job') and s['job'].get('stage') not in ('succeeded','cancelled'):
                    raise RuntimeError('Finish the current OTA task before saving the baseline')
                if not marker.exists() and not marker.is_symlink():
                    fd=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
                    with os.fdopen(fd,'wb') as stream:
                        stream.write(os.urandom(1024**2));stream.flush();os.fsync(stream.fileno())
                    fsync_dir(marker.parent)
                if marker.is_symlink() or not marker.is_file() or marker.stat().st_size!=1024**2:
                    raise RuntimeError('The existing DATA marker is unexpected; inspect it before continuing')
                atomic_json(baseline,current())
            if args.compare:
                if not baseline.is_file() or baseline.is_symlink():raise RuntimeError('The saved baseline is missing or unsafe')
                old=json.loads(baseline.read_text());now=current()
                comparison={name:old.get(name)==value for name,value in now.items()}
                comparison['DATA_marker_present']=bool(now['marker_sha256'])
                for name,value in comparison.items():check('preserved '+name,value,'baseline comparison')
        k3s_policy=Path('/etc/image-framework/k3s.json')
        if k3s_policy.is_file():
            from kube_runtime import installed,runtime
            kp=json.loads(k3s_policy.read_text());installation=installed(kp)
            if installation['desired']['enabled']:
                check('selected K3s installation',installation['ready'])
                check('K3s service, CRI and local API',runtime(kp)['ready'])
    elif args.baseline or args.compare:raise RuntimeError('Run baseline/compare in the SATA normal system')
    report={'schema_version':1,'version':'1.2.1','mode':s.get('mode'),'board':s.get('board'),
            'epoch':time.time(),'kernel':kernel,'management':management,'checks':checks,
            'comparison':comparison,'passed':all(row['passed'] for row in checks),
            'hardware_acceptance_signed':False,'pending':['physical reboot/cold-start/power-loss matrix',
                '24-hour load','cross-node CPU quota/seccomp/network-policy/swap workload checks']}
    destination=folder/(s['mode']+'-1.3-'+str(time.time_ns())+'.json')
    atomic_json(destination,report)
    print(json.dumps({'checks':checks,'passed':report['passed'],'report':str(destination),
                      'hardware_acceptance_signed':False},indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as error:
        print(json.dumps({'ok':False,'error':str(error),'hardware_acceptance_signed':False}),file=sys.stderr)
        raise SystemExit(2)
