#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Refresh paired roots and install an independently verified rebuilt kernel."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def rebase(project,ota_source,input_roots,boot_manifest,work,watchdog_binary,board,release,epoch,kernel_work,resume=False,apt_proxy=None,offline_dependencies=False):
    project,ota_source,input_roots,work,kernel_work=map(lambda p:Path(p).resolve(),
        (project,ota_source,input_roots,work,kernel_work))
    sys.path.insert(0,str(project))
    from framework.kernel_baseline import validate,requirements,module_manifest
    import urllib.parse
    if apt_proxy:
        parsed=urllib.parse.urlsplit(apt_proxy)
        if parsed.scheme not in ('http','https') or parsed.username or parsed.password or not parsed.hostname:
            raise ValueError('Build proxy must be an HTTP(S) endpoint without embedded credentials')
    tools=load('rebase_root_tools',project/'tools/build/rootfs.py')
    old=load('refresh_fixed_roots',project/'tools/build/refresh-fixed-boot-roots.py')
    result=json.loads((kernel_work/'KERNEL-RESULT.json').read_text())
    validation=validate(project,kernel_work/'kernel.config')
    tools.require(validation['config_sha256']==result['config_sha256'],'Final kernel configuration changed')
    tools.require(module_manifest(kernel_work/'modules',result['kernel_release'])['sha256']==result['modules_sha256'],
                  'Installed kernel modules differ from the build receipt')
    image=kernel_work/'obj/arch/arm64/boot/Image'
    dtb=kernel_work/('obj/arch/arm64/boot/dts/'+old.DTBS[board].removeprefix('dtb/'))
    for field,path in (('image_sha256',image),('dtb_sha256',dtb)):
        tools.require(tools.digest(path)==result[field],'Rebuilt kernel pairing changed: '+field)
    binding={'input_roots':str(input_roots),'board':board,'release':release,'kernel':result}
    if work.exists():
        tools.require(resume and (work/'rebase-inputs.json').is_file() and
                      json.loads((work/'rebase-inputs.json').read_text())==binding,
                      'Existing roots need --resume and exactly matching board/kernel inputs')
    else:
        old.refresh(project,ota_source,input_roots,boot_manifest,work,watchdog_binary,board,release,epoch)
        tools.save(work/'rebase-inputs.json',binding)
    previous=json.loads(Path(boot_manifest).read_text())
    shutil.copy2(input_roots/'boot/uInitrd',work/'boot/recovery-initramfs')
    shutil.copy2(input_roots/'boot/Image',work/'boot/recovery-kernel')
    shutil.copy2(input_roots/'boot'/old.DTBS[board],work/'boot/recovery-dtb')
    shutil.copy2(image,work/'boot/Image')
    destination=work/'boot'/old.DTBS[board];destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(dtb,destination)
    packages=('python3-yaml','cryptsetup','ipset','nftables','conntrack','ethtool')
    kernel=result['kernel_release']
    for role in old.ROLES:
        root=work/role
        tools.require(root.resolve().is_relative_to(work) and not root.is_symlink(),'Root preparation must not modify linked input roots')
        tools.overlay(root,project/'components/common/rootfs-overlay')
        tools.overlay(root,project/'adapters/boards'/board/'rootfs-overlay')
        if not role.endswith('rescue'):
            tools.overlay(root,project/'components/k3s/rootfs-overlay')
        tools.run('python3',ota_source/'tools/install-runtime.py','--root',root,
                  '--watchdog-binary',watchdog_binary,'--bootstrap-hooks')
        import runpy
        runpy.run_path(str(root/'usr/lib/armhpc/command_layout.py'))['finalize'](
            root,'rescue' if role.endswith('rescue') else 'normal')
        for item in (root/'usr/lib/modules').iterdir():
            tools.require(item.is_dir() and not item.is_symlink() and item.parent==root/'usr/lib/modules',
                          'Unexpected module directory')
            shutil.rmtree(item)
        shutil.copytree(kernel_work/'modules/lib/modules',root/'usr/lib/modules',dirs_exist_ok=True,symlinks=True)
        modules=root/'usr/lib/modules'/kernel
        for name in ('build','source'):(modules/name).unlink(missing_ok=True)
        (modules/'armhpc-kernel-build-id').write_text(result['image_sha256']+'\n')
        shutil.copy2(kernel_work/'kernel.config',modules/'config')
        subprocess.run(['depmod','-b',str(root),'-a',kernel],check=True)
        tools.save(root/'etc/image-framework/kernel-requirements.json',requirements(project))
        tools.save(root/'etc/image-framework/runtime-requirements.json',{
            'cgroup_mode':'v2','swap_default':'disabled','bootargs_required':['systemd.unified_cgroup_hierarchy=1']})
        tools.save(root/'etc/armhpc/release.json',{'release':release,'target':'rescue' if role.endswith('rescue') else 'system','kernel':kernel})
        for name,value in (('kernel',result['image_sha256']),('dtb',result['dtb_sha256'])):
            (root/'etc/armhpc'/('anchor-'+name+'-sha256')).write_text(value+'\n')
        policy_path=root/'etc/armhpc/rescue.json'
        if policy_path.is_file():
            policy=json.loads(policy_path.read_text());policy['kernel_release']=kernel
            tools.save(policy_path,policy)
        else:tools.require(not role.endswith('rescue'),'Rescue root is missing its board recovery policy')
        fstab=root/'etc/fstab';lines=[]
        for line in fstab.read_text().splitlines():
            fields=line.split()
            if len(fields)>=4 and not line.lstrip().startswith('#') and fields[1]=='/data':
                fields[3]='defaults,noatime,nofail,x-systemd.device-timeout=10s';line=' '.join(fields)
            lines.append(line)
        fstab.write_text('\n'.join(lines)+'\n')
        for name in ('swap.target',):
            path=root/'etc/systemd/system'/name
            if path.is_symlink() and os.readlink(path)=='/dev/null':path.unlink()
        guard=root/'usr/sbin/policy-rc.d';previous_guard=guard.read_bytes() if guard.is_file() else None
        guard.write_text('#!/bin/sh\nexit 101\n');guard.chmod(0o755)
        resolv=root/'etc/resolv.conf';link=os.readlink(resolv) if resolv.is_symlink() else None
        saved_resolv=resolv.read_bytes() if resolv.is_file() and not link else None
        if link:resolv.unlink()
        resolv.write_text(Path('/etc/resolv.conf').read_text())
        shutil.copy2('/usr/bin/qemu-aarch64-static',root/'usr/bin/qemu-aarch64-static')
        try:
            with tools.chroot_mounts(root):
                environment=['env','DEBIAN_FRONTEND=noninteractive']
                if apt_proxy:environment+=['http_proxy='+apt_proxy,'https_proxy='+apt_proxy]
                options=['-o','Acquire::Retries=1','-o','Acquire::http::Timeout=20','-o','Acquire::https::Timeout=20']
                if offline_dependencies:
                    installed=tools.run('chroot',root,'dpkg-query','-W','-f=${Package}\t${Status}\t${Version}\n',
                                        *packages,capture=True)
                    rows=[line.split('\t') for line in installed.splitlines()]
                    tools.require(len(rows)==len(packages) and {row[0] for row in rows}==set(packages) and
                                  all(len(row)==3 and row[1]=='install ok installed' and row[2] for row in rows),
                                  'Offline dependency mode requires all declared packages to be installed')
                    tools.save(root/'usr/share/doc/armhpc/OFFLINE-DEPENDENCIES.json',
                               {'source':'verified input root','packages':{row[0]:row[2] for row in rows}})
                else:
                    tools.run('chroot',root,*environment,'apt-get',*options,'-o','APT::Update::Error-Mode=any','update')
                    tools.run('chroot',root,*environment,'apt-get',*options,'install','-y','--no-install-recommends',*packages)
                    tools.run('chroot',root,'apt-get','clean')
        finally:
            if previous_guard is not None:guard.write_bytes(previous_guard)
            else:guard.unlink(missing_ok=True)
            if link:resolv.unlink();resolv.symlink_to(link)
            elif saved_resolv is not None:resolv.write_bytes(saved_resolv)
        units=['armhpc-management-ready.service','armhpc-health.timer']
        if not role.endswith('rescue'):
            units+=['armhpc-state.service','armhpc-runtime.service','armhpc-swap.service','armhpc-auth-recovery.service','armhpc-firewall.service']
        tools.run('systemctl','--root='+str(root),'disable','systemd-networkd-wait-online.service')
        tools.run('systemctl','--root='+str(root),'enable',*units)
        document=root/'usr/share/doc/armhpc/KERNEL-BUILD.json';document.parent.mkdir(parents=True,exist_ok=True)
        installed_modules=module_manifest(root,kernel)
        tools.save(document,{**result,'installed_modules_sha256':installed_modules['sha256']})
        tools.save(document.with_name('MODULES.json'),installed_modules)
    normal=work/'normal'
    (normal/'boot'/('config-'+kernel)).write_text((kernel_work/'kernel.config').read_text())
    with tools.chroot_mounts(normal):tools.run('chroot',normal,'update-initramfs',
        '-u' if (normal/'boot'/('initrd.img-'+kernel)).is_file() else '-c','-k',kernel)
    raw=normal/'boot'/('initrd.img-'+kernel)
    entries=tools.run('lsinitramfs',raw,capture=True).splitlines()
    for required in ('scripts/local-top/armhpc-ota-anchor','scripts/armhpc','usr/lib/armhpc-ota/boot-entry.py'):
        tools.require(any(p.endswith(required) for p in entries),'Initramfs integration is missing: '+required)
    tools.require(any('/'+kernel+'/' in p and 'modules.dep' in p for p in entries),'Initramfs has no matching module dependency index')
    shutil.copy2(raw,work/'boot/uInitrd')
    for role in old.ROLES:
        root=work/role
        for entry in (root/'boot').iterdir():
            if entry.is_file() or entry.is_symlink():entry.unlink()
        if role.endswith('rescue'):
            fallback=root/'usr/lib/armhpc-ota/boot';fallback.mkdir(parents=True,exist_ok=True)
            for name,path in (('kernel',image),('dtb',dtb),('initramfs',work/'boot/uInitrd')):
                shutil.copy2(path,fallback/name)
        (root/'usr/bin/qemu-aarch64-static').unlink(missing_ok=True)
    current={'release':release,'kernel_release':kernel,'files':{
        kind:{'sha256':tools.digest(path)} for kind,path in
        (('kernel',work/'boot/Image'),('dtb',destination),('initramfs',work/'boot/uInitrd'))}}
    tools.save(work/'boot-manifest.json',current)
    tools.save(work/'preparation.json',{'release':release,'rebuilt_kernel':result,'previous_boot':previous,
        'roles':list(old.ROLES),'hardware_verified':False,'initramfs_entries':len(entries),
        'source_commits':{p.name:subprocess.check_output(['git','-C',str(p),'rev-parse','HEAD'],text=True).strip()
                          for p in (project,ota_source)}})
    return current


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('project','ota-source','input-roots','boot-manifest','work','watchdog-binary','kernel-work'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--board',choices=('wxy-oect-original','wxy-oesp-v22'),required=True)
    parser.add_argument('--release',required=True);parser.add_argument('--epoch',type=int,required=True)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--apt-proxy',help='Build-only proxy; not written into target configuration')
    parser.add_argument('--offline-dependencies',action='store_true',help='Verify declared packages in the input root without network updates')
    print(json.dumps(rebase(**vars(parser.parse_args())),indent=2))
