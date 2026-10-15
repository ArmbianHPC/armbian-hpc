#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Verify an offline ARM64 Bookworm root without booting it or opening devices."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def verify(project,ota_source,root,output):
    project,ota_source,root,output=map(lambda p:Path(p).resolve(),(project,ota_source,root,output))
    if os.geteuid()!=0 or root==Path('/') or not (root/'etc/debian_version').is_file():
        raise ValueError('Select an offline Debian root as Linux root')
    spec=importlib.util.spec_from_file_location('offline_root_tools',project/'tools/build/rootfs.py')
    tools=importlib.util.module_from_spec(spec);spec.loader.exec_module(tools)
    records=[]
    def run(name,args,expected=0,**kwargs):
        result=subprocess.run(list(map(str,args)),capture_output=True,text=True,timeout=180,**kwargs)
        record={'check':name,'passed':result.returncode==expected,'returncode':result.returncode,
                'stdout':result.stdout[-6000:],'stderr':result.stderr[-6000:]}
        records.append(record)
        if not record['passed']:raise RuntimeError(name+' failed: '+record['stderr'][-1000:])
        return result.stdout
    helper=root/'usr/bin/qemu-aarch64-static';created=not helper.exists()
    if created:shutil.copy2('/usr/bin/qemu-aarch64-static',helper)
    temporary=Path(tempfile.mkdtemp(prefix='armhpc-offline-',dir=root/'tmp'))
    try:
        receipt=json.loads((root/'usr/share/doc/armhpc/KERNEL-BUILD.json').read_text())
        module_record=root/'usr/share/doc/armhpc/MODULES.json'
        if module_record.is_file():
            import hashlib
            inventory=json.loads(module_record.read_text())
            for name,expected in inventory['files'].items():
                path=root/'lib/modules'/receipt['kernel_release']/name
                if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                    raise RuntimeError('Installed module inventory differs: '+name)
            records.append({'check':'installed kernel module byte inventory','passed':True,'files':len(inventory['files'])})
        with tools.chroot_mounts(root):
            version=run('Bookworm Python 3.11',['chroot',root,'python3','--version'])
            if not version.startswith('Python 3.11.'):raise RuntimeError('Target Python is not Bookworm 3.11')
            run('installed runtime imports',['chroot',root,'python3','-c',
                'import sys;sys.path[:0]=["/usr/lib/armhpc","/usr/lib/armhpc-ota"];'
                'import yaml,cryptography,armhpc_ota.engine,node_state,network_ready,swap_config,auth_config,firewall_config'])
            run('device zstd executable',['chroot',root,'zstd','--version'])
            run('installed Python syntax',['chroot',root,'python3','-c',
                'from pathlib import Path;'
                'files=[p for d in ("/usr/lib/armhpc","/usr/lib/armhpc-ota") for p in Path(d).rglob("*.py")];'
                '[compile(p.read_bytes(),str(p),"exec") for p in files];print(len(files))'])
            for scope in ('common','k3s'):
                relative=Path('components')/scope/'rootfs-overlay/usr/lib/armhpc'
                shutil.copytree(project/relative,temporary/relative,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            testdir=temporary/'tests';testdir.mkdir()
            for name in ('test_host_settings.py','test_node_foundation.py','test_node_profiles.py'):
                shutil.copy2(project/'tests'/name,testdir/name)
            guest='/'+str(temporary.relative_to(root))
            paths=[guest+'/tests',guest+'/components/common/rootfs-overlay/usr/lib/armhpc',
                   guest+'/components/k3s/rootfs-overlay/usr/lib/armhpc']
            run('Python 3.11 recovery/network/auth/profile regressions',['chroot',root,'env',
                'PYTHONPATH='+':'.join(paths),'python3','-m','unittest','discover','-v','-s',guest+'/tests'])
        units=['armhpc-firstboot.service','armhpc-state.service','armhpc-runtime.service','armhpc-swap.service',
               'armhpc-auth-recovery.service','armhpc-firewall.service','armhpc-management-ready.service',
               'armhpc-health.service','armhpc-health.timer']
        run('systemd service dependencies',['systemd-analyze','--root='+str(root),'verify',*units])
        library=project/'components/common/rootfs-overlay/usr/lib/armhpc';sys.path.insert(0,str(library))
        import firewall_config
        rules=firewall_config.render(firewall_config.settings('server-etcd',['192.0.2.0/24'],['10.10.0.0/24']))
        result=subprocess.run(['unshare','--net','chroot',str(root),'nft','--check','-f','-'],
                              input=rules,capture_output=True,text=True,timeout=15)
        unsupported=result.returncode and any(text in result.stderr for text in (
            'Operation not permitted','Operation not supported','Protocol not supported'))
        records.append({'check':'nftables syntax in isolated host-kernel network namespace',
                        'passed':None if unsupported else result.returncode==0,
                        'status':'not-executed' if unsupported else 'passed' if result.returncode==0 else 'failed',
                        'scope':'host kernel; target kernel boot remains pending','stderr':result.stderr[-1000:]})
        if result.returncode and not unsupported:
            raise RuntimeError('nftables syntax validation failed: '+result.stderr)
        report={'checks':records,'passed':all(item['passed'] is not False for item in records),
                'hardware_verified':False,'root':str(root)}
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(report,indent=2)+'\n')
        return report
    finally:
        if not temporary.is_relative_to(root/'tmp') or temporary.is_symlink():raise RuntimeError('Unsafe verification cleanup path')
        shutil.rmtree(temporary)
        if created:helper.unlink()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('project','ota-source','root','output'):parser.add_argument('--'+name,type=Path,required=True)
    print(json.dumps(verify(**vars(parser.parse_args())),indent=2))
