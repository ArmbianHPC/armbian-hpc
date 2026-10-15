#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Prepare fresh Bookworm ARM64 userspace and paired OESP system/Rescue roots."""
import argparse, importlib.util, json, os, runpy, shutil, subprocess, sys, time
from pathlib import Path

def save(path, value):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value,indent=2)+'\n')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('project','ota-source','work'):p.add_argument('--'+name,type=Path,required=True)
    for name in ('kernel-work','trust-public-key','admin-source','admin-password-hash-file'):p.add_argument('--'+name,type=Path)
    p.add_argument('--base-only',action='store_true');p.add_argument('--release');p.add_argument('--epoch',type=int)
    p.add_argument('--target',choices=('wxy-oesp-v22-base','wxy-oesp-v22-k3s'),default='wxy-oesp-v22-k3s')
    p.add_argument('--debian-keyring',type=Path,default=Path('/usr/share/keyrings/debian-archive-keyring.gpg'))
    p.add_argument('--proxy')
    a=p.parse_args();assert os.geteuid()==0
    work=a.work.resolve();assert work!=Path('/') and not (work/'.git').exists();work.mkdir(parents=True,exist_ok=True)
    for role in ('normal','rescue'):
        node=work/role/'etc/image-framework/node.json'
        if node.exists():assert json.loads(node.read_text())['target']==a.target,'Use a separate root work directory for each workload'
    log=(work/'rootfs-build.log').open('a',buffering=1)
    def run(*args,**kw):
        print('Running',str(args[0]),flush=True)
        env={**os.environ}
        if a.proxy: env.update(http_proxy=a.proxy,https_proxy=a.proxy)
        subprocess.run(list(map(str,args)),check=True,stdout=log,stderr=subprocess.STDOUT,env=env,**kw)
    base=work/'base'
    if not (work/'base-ready.json').exists():
        assert a.debian_keyring.is_file(),'Provide a trusted Debian archive public keyring'
        if not (work/'foreign-ready.json').exists():
            run('debootstrap','--keyring='+str(a.debian_keyring),'--foreign','--arch=arm64','--variant=minbase','--include=ca-certificates',
                'bookworm',base,'http://deb.debian.org/debian')
            save(work/'foreign-ready.json',{'complete':True})
        shutil.copy2('/usr/bin/qemu-aarch64-static',base/'usr/bin/qemu-aarch64-static')
        if not (work/'second-stage-ready.json').exists():
            run('chroot',base,'/debootstrap/debootstrap','--second-stage')
            save(work/'second-stage-ready.json',{'complete':True})
        policy=base/'usr/sbin/policy-rc.d';policy.write_text('#!/bin/sh\nexit 101\n');policy.chmod(0o755)
        (base/'etc/resolv.conf').write_text(Path('/etc/resolv.conf').read_text())
        (base/'etc/apt/sources.list').write_text('deb http://deb.debian.org/debian bookworm main\ndeb http://security.debian.org/debian-security bookworm-security main\ndeb http://deb.debian.org/debian bookworm-updates main\n')
        run('chroot',base,'apt-get','update')
        packages=['systemd-sysv','udev','openssh-server','sudo','python3','python3-cryptography','ca-certificates','util-linux',
            'e2fsprogs','zstd','tar','acl','attr','fdisk','gdisk','chrony','iproute2','iputils-ping','ethtool','usbutils','pciutils',
            'initramfs-tools','busybox','kexec-tools','rsync','curl','iptables','nftables','conntrack','socat','dosfstools',
            'unzip','xz-utils','cpio','systemd-resolved','dbus','locales','less','bash-completion','kmod','procps',
            'u-boot-tools','python3-yaml','cryptsetup','ipset']
        run('chroot',base,'env','DEBIAN_FRONTEND=noninteractive','apt-get','install','-y','--no-install-recommends',*packages)
        run('chroot',base,'apt-get','clean')
        save(work/'base-ready.json',{'distribution':'bookworm','architecture':'arm64','packages':packages})
    if a.base_only:return
    assert all((a.kernel_work,a.trust_public_key,a.release,a.epoch)) and (a.admin_source or a.admin_password_hash_file)
    project,ota=a.project.resolve(),a.ota_source.resolve();sys.path.insert(0,str(project))
    from framework.config import resolve,policy_for
    resolved=resolve(project,a.target,require_sources=False)
    k3s=resolved['workload']['component']=='k3s'
    spec=importlib.util.spec_from_file_location('rootfs_tools',project/'tools/build/rootfs.py');tools=importlib.util.module_from_spec(spec);spec.loader.exec_module(tools)
    kernel=json.loads((a.kernel_work/'KERNEL-RESULT.json').read_text());release=kernel['kernel_release']
    helper=work/'watchdog-guard'
    run('aarch64-linux-gnu-gcc','-O2','-Wall','-Wextra','-static',ota/'boot/watchdog-guard.c','-o',helper)
    boot=work/'boot';boot.mkdir(exist_ok=True)
    shutil.copy2(a.kernel_work/'obj/arch/arm64/boot/Image',boot/'Image')
    dtb=boot/'dtb/amlogic/meson-g12b-s922x-armhpc-oes1plus-v22.dtb';dtb.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(a.kernel_work/'obj/arch/arm64/boot/dts/amlogic/meson-g12b-s922x-armhpc-oes1plus-v22.dtb',dtb)
    roots=[]
    for role,name in (('system','normal'),('rescue','rescue')):
        root=work/name
        if not root.exists():run('cp','-a','--reflink=auto',base,root)
        tools.overlay(root,project/'components/common/rootfs-overlay')
        tools.overlay(root,project/'adapters/boards/wxy-oesp-v22/rootfs-overlay')
        firmware=[]
        for item in resolved['firmware']:
            source=project/item['path']
            assert source.is_file() and tools.digest(source)==item['sha256'],'Firmware input checksum differs'
            destination=root/item['destination'].lstrip('/')
            destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,destination)
            destination.chmod(0o644)
            notice=root/'usr/share/doc/armhpc-firmware'/Path(item['license_file']).name
            notice.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(project/item['license_file'],notice)
            firmware.append({'id':item['id'],'sha256':item['sha256'],'destination':item['destination'],
                             'license':item['license'],'source':item['acquire']['url']})
        save(root/'etc/armhpc/firmware-inventory.json',{'protocol':1,'files':firmware})
        if role=='rescue':tools.overlay(root,project/'components/rescue/rootfs-overlay')
        elif k3s:tools.overlay(root,project/'components/k3s/rootfs-overlay')
        shutil.copytree(a.kernel_work/'modules/lib/modules',root/'usr/lib/modules',dirs_exist_ok=True,symlinks=True)
        m=root/'usr/lib/modules'/release
        for n in ('build','source'):(m/n).unlink(missing_ok=True)
        (m/'armhpc-kernel-build-id').write_text(kernel['image_sha256']+'\n')
        run('chroot',root,'depmod','-a',release)
        save(root/'etc/image-framework/node.json',{'schema_version':1,'target':a.target,'board':'wxy-oesp-v22','soc':'amlogic-s922x','architecture':'arm64','workload':resolved['workload']['id'],'workload_component':'k3s' if k3s and role=='system' else 'none'})
        if k3s:save(root/'etc/image-framework/k3s.json',policy_for(resolved))
        else:(root/'etc/image-framework/k3s.json').unlink(missing_ok=True)
        save(root/'etc/image-framework/kernel-requirements.json',resolved['kernel_requirements'])
        save(root/'etc/image-framework/runtime-requirements.json',{'cgroup_mode':'v2','swap_enabled':False,'bootargs_required':['systemd.unified_cgroup_hierarchy=1']})
        save(root/'etc/armhpc/ota.json',{'profile':'wxy-oesp-v22','storage_layout':'oesp-mbr-squashfs-ab-v1','hardware_policy':'development'})
        save(root/'etc/armhpc/release.json',{'release':a.release,'target':role,'kernel':release})
        save(root/'etc/armhpc/image-build-time.json',{'epoch':a.epoch,'revision':a.release})
        (root/'etc/armhpc/anchor-kernel-sha256').write_text(kernel['image_sha256']+'\n')
        (root/'etc/armhpc/anchor-dtb-sha256').write_text(kernel['dtb_sha256']+'\n')
        save(root/'etc/armhpc/rescue.json',{'target':a.target,'kernel_release':release,'root_size_bytes':68719476736,
             'root_label':'OESP_ROOT','data_label':'OESP_DATA','rescue_label':'OESP_RESCUE_A','boot_label':'OESP_BOOT','boot_fstype':'vfat'})
        (root/'etc/fstab').write_text('LABEL=OESP_BOOT /boot vfat ro,noatime,umask=022 0 2\nLABEL=OTA_SHARED /var/lib/armhpc/ota ext4 defaults,noatime,nofail,x-systemd.device-timeout=5s 0 2\n'+('LABEL=OESP_ROOT / ext4 defaults,noatime 0 1\nLABEL=OESP_DATA /data ext4 defaults,noatime 0 2\n' if role=='system' else ''))
        run('python3',ota/'tools/install-runtime.py','--root',root,'--watchdog-binary',helper,'--bootstrap-hooks')
        pub=root/'etc/armhpc/ota-trust.d'/a.trust_public_key.name;pub.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(a.trust_public_key,pub)
        first=runpy.run_path(str(root/'usr/lib/armhpc/firstboot.py'))
        previous=a.admin_password_hash_file.read_text().strip() if a.admin_password_hash_file else next(line.split(':')[1] for line in (a.admin_source/'etc/shadow').read_text().splitlines() if line.startswith('root:'))
        first['prepare_password_profile'](root,previous)
        tools.reset_identity(root)
        save(root/'etc/armhpc/authentication.json',{'authentication':'root_password','username':'root'})
        runpy.run_path(str(root/'usr/lib/armhpc/command_layout.py'))['finalize'](root,'normal' if role=='system' else 'rescue')
        (root/'etc/hostname').write_text('armhpc\n' if role=='system' else 'armhpc-rescue\n')
        run('systemctl','--root='+str(root),'enable','systemd-networkd.service','systemd-resolved.service','chrony.service','ssh.service','armhpc-firstboot.service',
            'armhpc-clock-floor.service','armhpc-clock-checkpoint.timer','armhpc-clock-shutdown.service','armhpc-phy-leds.service','armhpc-fan.service','armhpc-led.service',
            'armhpc-performance.service','armhpc-performance.timer')
        units=['armhpc-management-ready.service','armhpc-health.timer']
        if role=='system':units+=['armhpc-state.service','armhpc-runtime.service','armhpc-swap.service','armhpc-auth-recovery.service','armhpc-firewall.service']
        run('systemctl','--root='+str(root),'disable','systemd-networkd-wait-online.service')
        run('systemctl','--root='+str(root),'enable',*units)
        resolv=root/'etc/resolv.conf';resolv.unlink();resolv.symlink_to('/run/systemd/resolve/stub-resolv.conf')
        roots.append(root)
        if role=='rescue':
            factory=root/'usr/share/armhpc/factory';factory.parent.mkdir(parents=True,exist_ok=True)
            if not factory.exists() and not factory.is_symlink():factory.symlink_to('/var/lib/armhpc/ota/factory')
    defaults=work/'rescue-defaults';defaults.mkdir(mode=0o700,exist_ok=True)
    for relative in ('etc/shadow','etc/armhpc/authentication.json'):
        to=defaults/relative;to.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(work/'rescue'/relative,to)
    for root in roots:shutil.copytree(defaults,root/'etc/armhpc/rescue-defaults',dirs_exist_ok=True)
    normal=work/'normal';(normal/'boot'/('config-'+release)).write_text((a.kernel_work/'obj/.config').read_text())
    (normal/'etc/initramfs-tools/conf.d/compress').write_text('COMPRESS=zstd\nMODULES=most\n')
    with tools.chroot_mounts(normal):run('chroot',normal,'update-initramfs','-u' if (normal/'boot'/('initrd.img-'+release)).exists() else '-c','-k',release)
    shutil.copy2(normal/'boot'/('initrd.img-'+release),boot/'uInitrd')
    for root in roots:
        for entry in (root/'boot').iterdir():
            if entry.is_file() or entry.is_symlink():entry.unlink()
    fallback=work/'rescue/usr/lib/armhpc-ota/boot';fallback.mkdir(parents=True,exist_ok=True)
    for kind,path in [('kernel',boot/'Image'),('dtb',dtb),('initramfs',boot/'uInitrd')]:shutil.copy2(path,fallback/kind)
    public=work/'public';public.mkdir(exist_ok=True)
    for role,name in (('system','normal'),('rescue','rescue')):
        dest=public/role
        if dest.exists():
            assert dest.resolve().parent==public.resolve() and not dest.is_symlink()
            shutil.rmtree(dest)
        run('cp','-a','--reflink=auto',work/name,dest)
        for item in (dest/'etc/ssh').glob('ssh_host_*'):item.unlink()
        for relative in ('etc/armhpc/authentication.json','etc/image-framework/admin-provisioned.json','etc/armhpc/rescue-defaults'):
            item=dest/relative
            if item.is_dir():shutil.rmtree(item)
            else:item.unlink(missing_ok=True)
        rows=[line.split(':') for line in (dest/'etc/shadow').read_text().splitlines()]
        for row in rows:row[1]='!'
        (dest/'etc/shadow').write_text('\n'.join(':'.join(row) for row in rows)+'\n');(dest/'etc/shadow').chmod(0o600)
    result={'release':a.release,'kernel_release':release,'files':{r:{'sha256':tools.digest(path)} for r,path in [('kernel',boot/'Image'),('dtb',dtb),('initramfs',boot/'uInitrd')]}}
    save(work/'boot-manifest.json',result)
    print('Paired OESP roots prepared',flush=True)

if __name__=='__main__':main()
