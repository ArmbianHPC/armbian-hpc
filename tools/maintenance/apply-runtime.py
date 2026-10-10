#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Apply the r8 userspace repair to an existing r7 SATA installation and Rescue."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time


def run(*args, capture=False):
    result=subprocess.run(list(map(str,args)),check=True,text=True,capture_output=capture)
    return result.stdout.strip() if capture else None


def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def relative(value):
    path=Path(value)
    if path.is_absolute() or '..' in path.parts:raise ValueError('Unsafe update path')
    return path


def copy_atomic(source,target,backup,mode=None):
    if target.is_symlink():raise ValueError('Refusing a symlink update target: '+str(target))
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists() and not backup.exists():
        backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,backup)
    descriptor,temporary=tempfile.mkstemp(prefix='.'+target.name,dir=target.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source,temporary)
        if mode is not None:os.chmod(temporary,mode)
        os.replace(temporary,target)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


@contextmanager
def root_mounts(root):
    mounts=[]
    try:
        for name in ('dev','proc','sys'):
            target=root/name;target.mkdir(exist_ok=True)
            if subprocess.run(['mountpoint','-q',str(target)]).returncode==0:
                raise RuntimeError('Update build mount already active: '+str(target))
            run('mount','--bind','/'+name,target);mounts.append(target)
        yield
    finally:
        for path in reversed(mounts):run('umount',path)


def install_package(root,package,version):
    active=root==Path('/')
    policy=root/'usr/sbin/policy-rc.d'
    if policy.is_symlink():raise ValueError('policy-rc.d must be an ordinary file')
    old=policy.read_bytes() if policy.exists() else None
    old_mode=policy.stat().st_mode&0o777 if policy.exists() else None
    policy.write_text('#!/bin/sh\nexit 101\n');policy.chmod(0o755)
    environment=os.environ.copy();environment['DEBIAN_FRONTEND']='noninteractive'
    try:
        if active:
            subprocess.run(['dpkg','--force-confold','--install',str(package)],check=True,env=environment)
            installed=run('dpkg-query','-W','-f=${Version}','chrony',capture=True)
        else:
            staged=root/'tmp/armhpc-clock-package.deb';shutil.copyfile(package,staged)
            try:
                with root_mounts(root):
                    subprocess.run(['chroot',str(root),'dpkg','--force-confold','--install','/tmp/armhpc-clock-package.deb'],check=True,env=environment)
                    installed=run('chroot',root,'dpkg-query','-W','-f=${Version}','chrony',capture=True)
            finally:staged.unlink(missing_ok=True)
        if installed!=version:raise RuntimeError('Installed chrony version differs from bundle')
    finally:
        if old is None:policy.unlink()
        else:policy.write_bytes(old);policy.chmod(old_mode)


def update_root(root,bundle,manifest,rescue):
    backup=root/'var/lib/armhpc/updates'/manifest['update_id']/'before'
    install_package(root,bundle/manifest['clock_package']['path'],manifest['clock_package']['version'])
    for item in manifest['overlay']:
        if item['scope']=='rescue' and not rescue:continue
        path=relative(item['target'])
        copy_atomic(bundle/item['source'],root/path,backup/path,item['mode'])
    if rescue:
        required=sum((bundle/item['source']).stat().st_size for item in manifest['factory'])+128*1024**2
        space=os.statvfs(root)
        if space.f_bavail*space.f_frsize<required:raise RuntimeError('Rescue needs free space for atomic factory archive replacement')
        for item in manifest['factory']:
            path=relative(item['target'])
            # Large factory inputs are already recoverable in the old IMG; do not
            # duplicate archives in the small Rescue filesystem backup directory.
            target=root/path;target.parent.mkdir(parents=True,exist_ok=True)
            descriptor,temporary=tempfile.mkstemp(prefix='.'+target.name,dir=target.parent);os.close(descriptor)
            try:
                shutil.copyfile(bundle/item['source'],temporary)
                if digest(temporary)!=item['sha256']:raise RuntimeError('Factory copy checksum mismatch')
                os.replace(temporary,target)
            finally:
                if os.path.exists(temporary):os.unlink(temporary)
    epoch_file=root/'usr/lib/clock-epoch'
    os.utime(epoch_file,(manifest['clock_floor_epoch'],manifest['clock_floor_epoch']))
    run('systemctl','--root',root,'enable','armhpc-clock-floor.service','armhpc-clock-checkpoint.timer','chrony.service')
    run('sync','-f',root)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true',help='Apply the checked update to active SATA and inactive Rescue.')
    args=parser.parse_args()
    if Path('/usr/local/sbin/armhpc-kube').exists() or Path('/usr/local/sbin/armhpc-rescue').exists():
        raise RuntimeError('This historical r8 bundle cannot update a consolidated command interface')
    bundle=Path(__file__).resolve().parent
    manifest=json.loads((bundle/'manifest.json').read_text())
    if os.geteuid()!=0:parser.error('Run as root')
    if Path('/etc/armhpc/rescue-environment').exists():parser.error('Run this bundle from the installed SATA system')
    if os.uname().release!=manifest['kernel_release']:raise RuntimeError('Kernel differs from bundle')
    for item in [*manifest['overlay'],*manifest['factory'],manifest['clock_package']]:
        path=bundle/relative(item.get('source',item.get('path')))
        if not path.is_file() or path.is_symlink() or digest(path)!=item['sha256']:
            raise RuntimeError('Bundle file checksum mismatch: '+str(path))
    for name,checksum in manifest['boot_files_sha256'].items():
        if digest(Path('/boot')/relative(name))!=checksum:
            raise RuntimeError('Current BOOT differs from the tested r7 baseline: '+name)
    print('更新 active SATA 的 ARMHPC 脚本及校时组件，并更新 eMMC Rescue 和工厂归档。')
    print('BOOT 文件、分区表、/data、SSH 主机密钥、machine-id 和现有密码保持原样。')
    if not args.execute:
        print('校验通过；添加 --execute 应用更新。');return
    original=load(Path('/usr/lib/armhpc/firstboot.py'),'armhpc_current_firstboot')
    with original.identity_store() as (rescue_root,disk,policy):
        # The shared-identity mount is noexec. Permit execution only while dpkg
        # configures the verified package in this inactive Rescue filesystem.
        run('mount','-o','remount,rw,nosuid,nodev,exec',rescue_root)
        try:update_root(rescue_root,bundle,manifest,True)
        finally:run('mount','-o','remount,rw,nosuid,nodev,noexec',rescue_root)
    update_root(Path('/'),bundle,manifest,False)
    run('systemctl','daemon-reload')
    run('systemctl','start','armhpc-clock-floor.service')
    run('systemctl','restart','chrony.service')
    run('systemctl','start','armhpc-clock-checkpoint.timer')
    current=load(Path('/usr/lib/armhpc/firstboot.py'),'armhpc_updated_firstboot')
    receipt=json.loads(Path('/run/armhpc/network-identity.json').read_text())
    hostname=current.configure_hostname(receipt['mac'])
    run('/usr/bin/python3','/usr/lib/armhpc/phy_leds.py')
    print('更新完成，正常系统主机名：'+hostname)
    print('新 SSH 会话会显示新主机名。chronyc tracking 可检查联网校时状态。')


if __name__=='__main__':main()
