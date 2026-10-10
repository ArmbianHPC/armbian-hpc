# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Build preconstructed same-kernel OTA payloads from explicit prepared roots, never a full IMG."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--previous',type=Path,required=True);p.add_argument('--work',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--framework',type=Path,required=True);p.add_argument('--ota-source',type=Path,required=True)
    p.add_argument('--release',required=True);p.add_argument('--sequence',type=int,required=True);p.add_argument('--boot-manifest',type=Path,required=True)
    a=p.parse_args(); repo=a.ota_source;sys.path.insert(0,str(repo/'src'))
    from armhpc_ota.publish import filesystem,validate_staging
    from armhpc_ota.io import sha256,atomic_json
    from armhpc_ota.metadata import validate_manifest
    assert os.geteuid()==0 and a.work.is_absolute() and a.work!=Path('/')
    assert not a.work.exists(),'Use a new work directory; never overwrite a staging tree'
    assert not a.output.exists(),'Use a new output directory'
    a.work.mkdir(parents=True);a.output.mkdir(parents=True)
    from armhpc_ota import __version__
    inputs=json.loads(a.boot_manifest.read_text());kernel=inputs['kernel_release']
    expected={k:v['sha256'] for k,v in inputs['files'].items()}
    boot={'kernel':a.previous/'boot/Image','dtb':a.previous/'boot/dtb/rockchip/rk3566-wxy-oec-turbo-4g.dtb',
          'initramfs':a.previous/'boot'/('initrd.img-'+kernel)}
    assert all(sha256(v)==expected[k] for k,v in boot.items()),'Paired boot bytes changed'
    roots={}
    for target in ('system','rescue'):
        root=a.work/target;subprocess.run(['cp','-a','--reflink=auto',str(a.previous/'public'/target),str(root)],check=True);root.chmod(0o755)
        helper=a.previous/'public'/target/'usr/lib/armhpc-ota/watchdog-guard'
        subprocess.run(['python3',str(repo/'tools/install-runtime.py'),'--root',str(root),'--watchdog-binary',str(helper)],check=True)
        shutil.copy2(a.framework/'components/common/rootfs-overlay/usr/lib/armhpc/clock_floor.py',root/'usr/lib/armhpc/clock_floor.py')
        (root/'usr/lib/armhpc/clock_floor.py').chmod(0o644)
        atomic_json(root/'etc/armhpc/release.json',{'release':a.release,'target':target,'kernel':kernel})
        atomic_json(root/'etc/armhpc/ota-runtime.json',{'version':__version__,'factory_payload':True})
        validate_staging(root,target,a.release,kernel);roots[target]=root
    artifacts=[];files={}
    for target in ('system','rescue'):
        if target=='system':
            image,compressed=filesystem(roots[target],a.output,target,a.release,kernel)
            payload=compressed;encoding='ext4.zstd';expanded_size=image.stat().st_size;expanded_sha=sha256(image)
        else:
            payload=a.output/'rescue.squashfs'
            subprocess.run(['mksquashfs',str(roots[target]),str(payload),'-noappend','-comp','xz','-b','1M','-Xdict-size','100%',
                            '-processors','4','-all-time','1791417600','-no-progress'],check=True)
            encoding='squashfs';expanded_size=payload.stat().st_size;expanded_sha=sha256(payload)
            assert expanded_size<=512*1024**2-65536,'Rescue does not fit its unchanged slot'
        role=target+'-rootfs';digest=sha256(payload)
        artifacts.append({'id':role,'role':role,'sha256':digest,'size':payload.stat().st_size,'url':'https://pending.invalid/'+digest,
                          'encoding':encoding,'expanded_size':expanded_size,'expanded_sha256':expanded_sha})
        files[role]=payload.name
        for name,source in boot.items():
            dest=a.output/(target+'-'+name);shutil.copy2(source,dest);role=target+'-'+name
            artifacts.append({'id':role,'role':role,'sha256':expected[name],'size':dest.stat().st_size,'url':'https://pending.invalid/'+role});files[role]=dest.name
    m={'kind':'release','protocol':1,'release':a.release,'sequence':a.sequence,'board':'wxy-oect-original','architecture':'arm64',
       'distribution':'bookworm','layout':'oect-squashfs-ab-v2','anchor_abi':'armhpc-anchor-squashfs-v2','minimum_agent':__version__,
       'kernels':{'system':kernel,'rescue':kernel},'packages':{'system':[],'rescue':[]},'artifacts':artifacts}
    for target,root in roots.items():
        inventory=subprocess.check_output(['chroot',str(root),'dpkg-query','-W','-f=${binary:Package}\t${Version}\t${db:Status-Status}\n'],text=True)
        m['packages'][target]=[{'name':r.split('\t')[0],'version':r.split('\t')[1]} for r in inventory.splitlines() if len(r.split('\t'))==3 and r.split('\t')[2]=='installed']
    validate_manifest(m);atomic_json(a.output/'prepared-release.json',{'validated_staging':True,'manifest':m,'files':files})
    atomic_json(a.output/'BUILD-RESULT.json',{'release':a.release,'agent_version':__version__,'kernel_unchanged':True,'dtb_unchanged':True,
                'raw_initramfs_unchanged':True,'hardware_verified':False,'full_img_built':False,'rescue_bytes':expanded_size,
                'boot_sha256':expected,'prepared_descriptor':str(a.output/'prepared-release.json')})
    print(json.dumps({'prepared':str(a.output),'release':a.release,'hardware_verified':False}),flush=True)


if __name__=='__main__':main()
