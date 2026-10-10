# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Prepare explicit paired roots for the OECT SquashFS A/B image builder."""
from pathlib import Path
import argparse,importlib.util,json,os,shutil,subprocess,hashlib
parser=argparse.ArgumentParser(description=__doc__)
for n in ('project','ota-source','work','input-roots','trust-public-key','watchdog-binary','boot-manifest'):
    parser.add_argument('--'+n,type=Path,required=True)
parser.add_argument('--release',required=True);parser.add_argument('--epoch',type=int,required=True)
args=parser.parse_args();project=args.project.resolve(strict=True);ota=args.ota_source.resolve(strict=True)
work=args.work.resolve();old=args.input_roots.resolve(strict=True);key=args.trust_public_key.resolve(strict=True);helper=args.watchdog_binary.resolve(strict=True)
manifest=json.loads(args.boot_manifest.read_text());RELEASE=args.release;KERNEL=manifest['kernel_release'];EPOCH=args.epoch
assert os.geteuid()==0,'Run rootfs preparation on Linux as root'
assert not work.exists(),'Use a fresh output directory'
assert not any((p/'.git').exists() for p in (work,*work.parents)),'Build outside source repositories'
for role,name in (('kernel','Image'),('initramfs','uInitrd'),('dtb','dtb/rockchip/rk3566-wxy-oec-turbo-4g.dtb')):
    with (old/'boot'/name).open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
    assert actual==manifest['files'][role]['sha256'],'Input BOOT pairing differs: '+role
work.mkdir(parents=True)
def run(*a):subprocess.run(list(map(str,a)),check=True)
def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,indent=2)+'\n')
def load(p,n):s=importlib.util.spec_from_file_location(n,p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
def remove(p):
    assert p.parent.resolve().is_relative_to(work) and p!=work
    if p.is_dir() and not p.is_symlink():shutil.rmtree(p)
    else:p.unlink(missing_ok=True)
tools=load(project/'tools/build/rootfs.py','oect_rootfs_tools')
assert not (work/'normal').exists()
for n in ('normal','rescue','boot'):run('cp','-a','--reflink=auto',old/n,work/n)
boot=work/'boot'
for n in ('armhpc-ota-bootstrap','armhpc-anchor-candidate'):remove(boot/n)
kernel_hash=manifest['files']['kernel']['sha256']
defaults=work/'rescue-defaults'
for relative in ('etc/shadow','etc/armhpc/authentication.json'):
    p=defaults/relative;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(work/'rescue'/relative,p)
defaults.chmod(0o700)
for target,n in [('system','normal'),('rescue','rescue')]:
    root=work/n
    for relative in ('usr/share/armhpc/ota/bootstrap','usr/share/armhpc/factory','usr/lib/armhpc-ota/boot',
                     'etc/initramfs-tools/scripts/local-top/armhpc-root-select','etc/initramfs-tools/hooks/armhpc-root-selection',
                     'etc/initramfs-tools/scripts/local-top/armhpc-ota-anchor','etc/initramfs-tools/hooks/armhpc-ota-anchor',
                     'var/lib/armhpc/ota'):
        remove(root/relative)
    tools.overlay(root,project/'components/common/rootfs-overlay');tools.overlay(root,project/'adapters/boards/wxy-oect-original/rootfs-overlay')
    if target=='rescue':tools.overlay(root,project/'components/rescue/rootfs-overlay')
    for relative in ('etc/initramfs-tools/scripts/local-top/armhpc-root-select','etc/initramfs-tools/hooks/armhpc-root-selection'):remove(root/relative)
    tools.reset_identity(root)
    run('python3',ota/'tools/install-runtime.py','--root',root,'--watchdog-binary',helper,'--bootstrap-hooks')
    save(root/'etc/armhpc/ota.json',{'state':'/var/lib/armhpc/ota','cache':'/data/armhpc-ota/cache','trust':'/etc/armhpc/ota-trust.d',
         'profile':'wxy-oect-original','storage_layout':'oect-squashfs-ab-v2','hardware_policy':'development',
         'feed':'https://github.com/chens32/armbian-hpc/releases/latest/download/ota-index.json',
         'tools_source':'https://github.com/ArmbianHPC/armhpc-ota/releases'})
    save(root/'etc/armhpc/release.json',{'release':RELEASE,'target':target,'kernel':KERNEL})
    save(root/'etc/armhpc/image-build-time.json',{'epoch':EPOCH,'revision':RELEASE})
    (root/'etc/armhpc/anchor-kernel-sha256').write_text(kernel_hash+'\n')
    shutil.copytree(defaults,root/'etc/armhpc/rescue-defaults',dirs_exist_ok=True)
    shutil.copy2(key,root/'etc/armhpc/ota-trust.d'/key.name)
    for p in (root/'boot').iterdir():remove(p)
    if target=='rescue':
        f=root/'usr/share/armhpc/factory';f.parent.mkdir(parents=True,exist_ok=True);f.symlink_to('/var/lib/armhpc/ota/factory')
    (root/'etc/fstab').write_text('# Root is selected by the ARMHPC initramfs.\nLABEL=OECT_BOOT /boot ext4 ro,noatime 0 2\n'+
        ('LABEL=OECT_ROOT / ext4 defaults,noatime 0 1\nLABEL=OECT_DATA /data ext4 defaults,noatime 0 2\n' if target=='system' else '')+
        'LABEL=OTA_SHARED /var/lib/armhpc/ota ext4 defaults,noatime,nofail,x-systemd.device-timeout=5s 0 2\n')
    journal=root/'etc/systemd/journald.conf.d/armhpc-rescue-space.conf';journal.parent.mkdir(parents=True,exist_ok=True)
    if target=='rescue':journal.write_text('[Journal]\nSystemMaxUse=32M\nRuntimeMaxUse=16M\n')
run('rsync','-aHAX','--numeric-ids',str(boot)+'/',str(work/'normal/boot')+'/')
with tools.chroot_mounts(work/'normal'):run('chroot',work/'normal','update-initramfs','-u','-k',KERNEL)
shutil.copyfile(work/'normal/boot'/('initrd.img-'+KERNEL),boot/('initrd.img-'+KERNEL))
remove(boot/'uInitrd');(boot/'uInitrd').symlink_to('initrd.img-'+KERNEL)
# Same raw target initramfs is used by fixed entry and both first-install roots.
for p in (work/'normal/boot').iterdir():remove(p)
fallback=work/'rescue/usr/lib/armhpc-ota/boot';fallback.mkdir(parents=True,exist_ok=True)
for name,p in [('kernel',boot/'Image'),('initramfs',boot/'uInitrd'),('dtb',boot/'dtb/rockchip/rk3566-wxy-oec-turbo-4g.dtb')]:shutil.copy2(p,fallback/name)
public=work/'public';public.mkdir(exist_ok=True)
for target,n in [('system','normal'),('rescue','rescue')]:
    dest=public/target;run('cp','-a','--reflink=auto',work/n,dest)
    for p in (dest/'etc/ssh').glob('ssh_host_*'):remove(p)
    for r in ('etc/armhpc/authentication.json','etc/image-framework/admin-provisioned.json','root/.ssh','root/.bash_history','etc/armhpc/rescue-defaults'):remove(dest/r)
    rows=[r.split(':') for r in (dest/'etc/shadow').read_text().splitlines()]
    for row in rows:row[1]='!'
    (dest/'etc/shadow').write_text('\n'.join(':'.join(row) for row in rows)+'\n');(dest/'etc/shadow').chmod(0o600)
save(work/'boot-manifest.json',{'kernel_release':KERNEL,'files':{r:{'sha256':hashlib.file_digest((work/'boot'/n).open('rb'),'sha256').hexdigest()} for r,n in [('kernel','Image'),('initramfs','uInitrd'),('dtb','dtb/rockchip/rk3566-wxy-oec-turbo-4g.dtb')]}})
save(work/'preparation.json',{'release':RELEASE,'kernel':KERNEL,'input_boot':manifest,'agent_version':(ota/'VERSION').read_text().strip()})
print('Prepared paired and sanitized roots: '+str(work))
