#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Build an OECT kernel from pinned Linux, the retained board configuration and DTS."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

VERSION='6.12.111'
SOURCE_SHA256='9e59dc67624188fa12a6601f9598499cd6662a9066be572b59f935e3d7849810'


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def run(*args,**kwargs):subprocess.run(list(map(str,args)),check=True,**kwargs)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('project','source-archive','base-config','work'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--base-config-sha256',required=True)
    parser.add_argument('--jobs',type=int,default=8)
    args=parser.parse_args()
    if sha(args.source_archive)!=SOURCE_SHA256 or sha(args.base_config)!=args.base_config_sha256:
        raise ValueError('Kernel archive or retained board configuration differs')
    work=args.work.resolve()
    if work==Path('/') or any((p/'.git').is_dir() for p in (work,*work.parents)):
        raise ValueError('Use a build directory outside source repositories')
    work.mkdir(parents=True,exist_ok=True)
    source,obj=work/'source',work/'obj'
    if not source.exists():
        with tarfile.open(args.source_archive) as archive:archive.extractall(work,filter='data')
        (work/('linux-'+VERSION)).rename(source)
    patch_dir=args.project/'adapters/boards/wxy-oect-original/armbian/kernel-6.12.111'
    patches=[]
    for name in ('board-oect-usb2-peripheral-power.patch','board-oect-rtl8211f-ledsel.patch','board-oect-retained-drivers.patch'):
        patch=patch_dir/name
        marker=source/('.armhpc-'+name+'.sha256')
        checksum=sha(patch)
        if marker.exists():
            if marker.read_text().strip()!=checksum:raise ValueError('Board patch changed; use a fresh source directory')
        else:
            run('patch','-p1','--batch','--forward',cwd=source,input=patch.read_text(),text=True)
            marker.write_text(checksum+'\n')
        patches.append({'file':str(patch.relative_to(args.project)),'sha256':checksum})
    obj.mkdir(exist_ok=True)
    if not (obj/'.config').exists():shutil.copy2(args.base_config,obj/'.config')
    board=json.loads((args.project/'adapters/boards/wxy-oect-original.json').read_text())
    dts=args.project/board['boot']['dts']
    expected=next(item['sha256'] for item in board['inputs'] if item['path']==board['boot']['dts'])
    if sha(dts)!=expected:raise ValueError('OECT DTS differs from its reviewed source pin')
    destination=source/'arch/arm64/boot/dts/rockchip'/dts.name
    shutil.copy2(dts,destination)
    makefile=destination.parent/'Makefile'
    entry='dtb-$(CONFIG_ARCH_ROCKCHIP) += '+dts.stem+'.dtb'
    if entry not in makefile.read_text():
        with makefile.open('a') as stream:stream.write('\n'+entry+'\n')
    version=(args.project/'VERSION').read_text().strip()
    options=['ARCH=arm64','CROSS_COMPILE=aarch64-linux-gnu-','O='+str(obj),'LOCALVERSION=-armhpc-rk3566-'+version]
    sys.path.insert(0,str(args.project.resolve()))
    from framework.kernel_baseline import merge,validate,module_manifest
    merge(args.project,source,obj,run)
    run(source/'scripts/config','--file',obj/'.config','--disable','LOCALVERSION_AUTO',
        '--enable','PHYLIB_LEDS','--enable','SQUASHFS','--enable','SQUASHFS_XZ','--enable','SQUASHFS_XATTR',
        '--enable','RD_ZSTD','--enable','USB_CONFIGFS_ACM','--enable','USB_DWC2_DUAL_ROLE',
        '--enable','ARCH_ROCKCHIP','--enable','CPU_FREQ_GOV_PERFORMANCE')
    run('make',*options,'olddefconfig',cwd=source)
    configuration=validate(args.project,obj/'.config')
    env={**os.environ,'KBUILD_BUILD_USER':'armhpc','KBUILD_BUILD_HOST':'builder','KBUILD_BUILD_VERSION':'1'}
    run('make',*options,'-j'+str(args.jobs),'Image','modules','rockchip/'+dts.stem+'.dtb',cwd=source,env=env)
    release=subprocess.check_output(['make','-s',*options,'kernelrelease'],cwd=source,text=True).strip()
    modules=work/'modules'
    run('make',*options,'INSTALL_MOD_PATH='+str(modules),'INSTALL_MOD_STRIP=1','modules_install',cwd=source)
    inventory=module_manifest(modules,release)
    (work/'MODULES.json').write_text(json.dumps(inventory,indent=2)+'\n')
    shutil.copy2(obj/'.config',work/'kernel.config')
    result={**configuration,'kernel_release':release,'source_version':VERSION,'source_sha256':SOURCE_SHA256,'modules_sha256':inventory['sha256'],
            'base_config_sha256':args.base_config_sha256,'dts_source_sha256':expected,'patches':patches,
            'image_sha256':sha(obj/'arch/arm64/boot/Image'),
            'dtb_sha256':sha(obj/'arch/arm64/boot/dts/rockchip'/(dts.stem+'.dtb'))}
    (work/'KERNEL-RESULT.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
