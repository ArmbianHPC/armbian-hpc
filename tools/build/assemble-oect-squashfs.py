#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Prepartitioned direct-SquashFS Rescue A/B with compact, growable shared ext4."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


def run(*a,capture=False,accepted=(0,)):
    p=subprocess.run(list(map(str,a)),text=True,capture_output=capture)
    if p.returncode not in accepted:raise RuntimeError(str(a[0])+' failed: '+str(p.returncode))
    return p.stdout.strip() if capture else None
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,v):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,indent=2)+'\n')

def main():
    global KERNEL, RELEASE, STEM, EPOCH
    parser=argparse.ArgumentParser(description=__doc__)
    for n in ('project','ota-source','work','output','base-image','private-directory'):parser.add_argument('--'+n,required=True,type=Path)
    parser.add_argument('--release', required=True)
    parser.add_argument('--sequence', required=True, type=int)
    parser.add_argument('--boot-manifest', required=True, type=Path)
    parser.add_argument('--base-sha256', required=True)
    parser.add_argument('--base-provenance', required=True, type=Path)
    parser.add_argument('--key-id', required=True)
    parser.add_argument('--signing-key', required=True, type=Path)
    parser.add_argument('--epoch', required=True, type=int)
    parser.add_argument('--stem')
    parser.add_argument('--asset-base-url', default='https://payload.armhpc.invalid')
    parser.add_argument('--profile',default='wxy-oect-original')
    a=parser.parse_args();assert os.geteuid()==0
    inputs=json.loads(a.boot_manifest.read_text());KERNEL=inputs['kernel_release'];RELEASE=a.release;EPOCH=a.epoch
    sys.path.insert(0,str(a.ota_source/'src'))
    STEM=a.stem or ('ArmbianHPC_'+(a.project/'VERSION').read_text().strip()+'_wxy-oect_bookworm_'+KERNEL.split('-')[0]+'_squashfs-ab')
    project,ota,work,output,base,private=(getattr(a,n).resolve() for n in ('project','ota_source','work','output','base_image','private_directory'))
    assert work.is_absolute() and work!=Path('/') and all((work/n).is_dir() for n in ('normal','rescue','boot','public'))
    assert not output.is_relative_to(project) and not output.is_relative_to(ota) and not output.is_relative_to(work) and not a.signing_key.resolve().is_relative_to(output)
    sys.path.insert(0,str(ota/'src'));sys.path.insert(0,str(project))
    from framework.compatibility import check_sources
    compatibility=check_sources(ota,project)
    output.mkdir(parents=True,exist_ok=True);(output/'.incomplete').write_text('Assembly and boot validation pending\n')
    from armhpc_ota.config import Config
    from armhpc_ota.io import atomic_bytes
    from armhpc_ota.metadata import verify,validate_manifest,sign,anchor_digest
    from armhpc_ota.publish import bundle,index
    from armhpc_ota.squashfs import write_trailer,mark_ready,TRAILER_BYTES
    config=Config(profile=a.profile,storage_layout='oect-squashfs-ab-v2' if a.profile=='wxy-oect-original' else None)
    board=config.board();amlogic=board.get('partition_table')=='dos'
    disk_signature=None
    if amlogic:
        import secrets
        signature_file=work/'mbr-signature.txt'
        if not signature_file.exists():signature_file.write_text(format(secrets.randbits(32) or 1,'08x')+'\n')
        disk_signature=int(signature_file.read_text().strip(),16)
    comparison=json.loads((work/'compression/compression-comparison.json').read_text());selected=comparison['selected']
    boot=work/'boot';normal=work/'normal';rescue=work/'rescue';public=work/'public';shared=work/'shared'
    import runpy
    validate_environment=runpy.run_path(str(project/'components/common/rootfs-overlay/usr/lib/armhpc/command_layout.py'))['validate_environment']
    for root,mode in ((normal,'normal'),(rescue,'rescue'),(public/'system','normal'),(public/'rescue','rescue')):
        validate_environment(root,mode)
        assert (root/'usr/lib/armhpc-ota/armhpc_ota/__init__.py').is_file()
    key_id=a.key_id;key=a.signing_key.resolve()
    assert key.is_file();assert sha(base)==a.base_sha256
    dtb_relative='dtb/amlogic/meson-g12b-s922x-armhpc-oes1plus-v22.dtb' if amlogic else 'dtb/rockchip/rk3566-wxy-oec-turbo-4g.dtb'
    kernel_hash=sha(boot/'Image');dtb=boot/dtb_relative
    assert kernel_hash==inputs['files']['kernel']['sha256'] and sha(dtb)==inputs['files']['dtb']['sha256'] and sha((boot/'uInitrd').resolve())==inputs['files']['initramfs']['sha256']
    for root in (normal,rescue,public/'system',public/'rescue'):
        assert (root/'lib/modules'/KERNEL/'armhpc-kernel-build-id').read_text().strip()==kernel_hash
    manifest_path=output/'ota'/(RELEASE+'.manifest.json')
    if not manifest_path.exists():
        spec={'release':RELEASE,'sequence':a.sequence,'board':board['id'],'architecture':'arm64','layout':board['layout'],
              'anchor_abi':board['anchor_abi'],'minimum_agent':compatibility['minimum_agent'],'kernels':{'system':KERNEL,'rescue':KERNEL},'targets':{}}
        for target in ('system','rescue'):
            spec['targets'][target]={'rootfs':str(public/target),'kernel':str(boot/'Image'),'initramfs':str((boot/'uInitrd').resolve()),'dtb':str(dtb)}
        spec['targets']['rescue'].update(filesystem='squashfs',prebuilt_squashfs=str(work/'compression'/(selected['name']+'.squashfs')),
            source_fingerprint=comparison['source_fingerprint'],prebuilt_sha256=selected['sha256'],squashfs_options=selected['options'])
        save(output/'release-spec.json',spec)
        bundle(output/'release-spec.json',output/'ota',key,key_id,a.asset_base_url.rstrip('/'))
    manifest=validate_manifest(verify(manifest_path.read_bytes(),normal/'etc/armhpc/ota-trust.d'))
    sq=output/'ota/rescue.squashfs';root_item=next(x for x in manifest['artifacts'] if x['role']=='rescue-rootfs')
    assert sha(sq)==selected['sha256'] and sq.stat().st_size < min(board['rescue_a_sectors']*512-TRAILER_BYTES,board.get('rescue_payload_limit_bytes',board['rescue_a_sectors']*512-TRAILER_BYTES))
    index([{'release':RELEASE,'sequence':a.sequence,'board':board['id'],'architecture':'arm64','distribution':'bookworm',
            'url':a.asset_base_url.rstrip('/')+'/'+manifest_path.name,
            'size':manifest_path.stat().st_size,'sha256':sha(manifest_path)}],output/'ota/ota-index.json',key,key_id)
    # Original boot load addresses are preserved; the raw initramfs now owns root selection.
    append='root=PARTUUID=1f2a334c-eb25-4ab3-aadc-b38836beaa42 rootfstype=squashfs ro rootwait boot=armhpc net.ifnames=0 earlycon=uart8250,mmio32,0xfe660000 console=ttyS2,1500000 console=tty1 no_console_suspend systemd.unified_cgroup_hierarchy=1'
    (boot/'extlinux').mkdir(parents=True,exist_ok=True)
    (boot/'extlinux/extlinux.conf').write_text('LABEL ARMHPC\n  LINUX /Image\n  INITRD /uInitrd\n  FDT /dtb/rockchip/rk3566-wxy-oec-turbo-4g.dtb\n  APPEND '+append+'\n')
    script='''# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
echo ARMHPC direct SquashFS A/B raw initramfs
if test -z "${devtype}"; then setenv devtype mmc; fi
if test -z "${devnum}"; then setenv devnum 0; fi
if test -z "${distro_bootpart}"; then setenv distro_bootpart 1; fi
setenv kernel_addr_r 0x02000000
setenv fdt_addr_r 0x12000000
setenv ramdisk_addr_r 0x12180000
setenv bootargs "APPEND_ARGS"
if load ${devtype} ${devnum}:${distro_bootpart} ${kernel_addr_r} /Image; then
  if load ${devtype} ${devnum}:${distro_bootpart} ${fdt_addr_r} /dtb/rockchip/rk3566-wxy-oec-turbo-4g.dtb; then
    if load ${devtype} ${devnum}:${distro_bootpart} ${ramdisk_addr_r} /uInitrd; then
      setenv armhpc_initrd_size ${filesize}
      booti ${kernel_addr_r} ${ramdisk_addr_r}:${armhpc_initrd_size} ${fdt_addr_r}
    fi
  fi
fi
echo ARMHPC fixed entry load failed
'''.replace('APPEND_ARGS',append)
    if amlogic:
        append='root=PARTUUID='+format(disk_signature,'08x')+'-02 rootfstype=squashfs ro rootwait boot=armhpc net.ifnames=0 console=ttyAML0,115200 console=tty1 systemd.unified_cgroup_hierarchy=1'
        script='''# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
echo ARMHPC OES1PLUS v2.2
if test -z "${devtype}"; then setenv devtype mmc; fi
if test -z "${devnum}"; then setenv devnum 1; fi
setenv kernel_addr_r 0x08080000
setenv fdt_addr_r 0x08008000
setenv ramdisk_addr_r 0x13000000
setenv fdt_high 0x20000000
setenv bootargs "APPEND_ARGS"
if test "${devtype}" = "usb"; then setenv bootargs "${bootargs} armhpc.install-medium=usb"; fi
if fatload ${devtype} ${devnum}:1 ${kernel_addr_r} Image; then
  if fatload ${devtype} ${devnum}:1 ${fdt_addr_r} DTB_FILE; then
    if fatload ${devtype} ${devnum}:1 ${ramdisk_addr_r} uInitrd.uboot; then
      fdt addr ${fdt_addr_r}
      booti ${kernel_addr_r} ${ramdisk_addr_r} ${fdt_addr_r}
    fi
  fi
fi
echo ARMHPC verified entry load failed
'''.replace('APPEND_ARGS',append).replace('DTB_FILE',dtb_relative)
    if amlogic:
        # The vendor entry uses a legacy ramdisk header; OTA and slot copies stay raw.
        from framework.amlogic_boot import wrap_ramdisk, validate_boot_memory
        wrap_ramdisk(boot/'uInitrd', boot/'uInitrd.uboot', EPOCH)
        validate_boot_memory(boot/'Image', boot/dtb_relative, boot/'uInitrd.uboot')
    (boot/'boot.cmd').write_text(script)
    if amlogic:
        (boot/'extlinux/extlinux.conf').write_text('LABEL ARMHPC\n  LINUX /Image\n  INITRD /uInitrd\n  FDT /'+dtb_relative+'\n  APPEND '+append+'\n')
    run('mkimage','-A','arm','-O','linux','-T','script','-C','none','-n','ARMHPC SquashFS A/B','-d',boot/'boot.cmd',boot/'boot.scr')
    if amlogic:
        # The vendor USB path sources this file; it must not persist environment changes.
        usb_script='setenv devtype usb\nsetenv devnum 0\n'+script
        (boot/'aml_autoscript.cmd').write_text(usb_script)
        run('mkimage','-A','arm','-O','linux','-T','script','-C','none','-n','ARMHPC USB Rescue','-d',boot/'aml_autoscript.cmd',boot/'aml_autoscript')
    (boot/'armbianEnv.txt').write_text('fdtfile='+dtb_relative.removeprefix('dtb/')+'\narmhpc_mode=rescue\n')
    boot_hashes={p.relative_to(boot).as_posix():sha(p) for p in boot.rglob('*') if p.is_file() and not p.is_symlink()}
    if not shared.exists():
        shared.mkdir()
        (shared/'identity-root').mkdir(mode=0o700)
        for slot in ('a','b'):
            g=shared/'overlays'/slot/RELEASE;g.mkdir(parents=True)
            shutil.copytree(work/'rescue-defaults',g/'upper');(g/'upper').chmod(0o755);(g/'work').mkdir(mode=0o700)
            mark_ready(shared,slot,RELEASE,root_item['sha256'])
        store=shared/'baselines/releases'/RELEASE;store.mkdir(parents=True)
        cached=shared/'cache';cached.mkdir()
        for item in manifest['artifacts']:
            source=output/'ota'/item['url'].rsplit('/',1)[-1];dest=store/item['sha256']
            if not dest.exists():shutil.copy2(source,dest)
            if not (cached/item['sha256']).exists():os.link(dest,cached/item['sha256'])
        shutil.copy2(manifest_path,store/'manifest.json')
        (shared/'manifests').mkdir();shutil.copy2(manifest_path,shared/'manifests'/(RELEASE+'.json'))
        refs={}
        for target in ('system','rescue'):
            ref={'release':RELEASE,'kernel_release':KERNEL};g=shared/'kernels'/RELEASE/target;g.mkdir(parents=True)
            for kind in ('kernel','initramfs','dtb'):
                item=next(i for i in manifest['artifacts'] if i['role']==target+'-'+kind)
                os.link(store/item['sha256'],g/kind);ref[kind]={'path':(g/kind).relative_to(shared).as_posix(),'sha256':item['sha256']}
            refs[target]=ref
        anchor_items=[{'id':n,'sha256':sha(boot/p),'size':(boot/p).stat().st_size} for n,p in
                      [('kernel','Image'),('initramfs','uInitrd'),('dtb',dtb_relative)]]
        save(shared/'control.json',{'protocol':1,'anchor_abi':board['anchor_abi'],'anchor_sha256':anchor_digest(anchor_items),
             'device_id':None,'confirmed':{'rescue':{'slot':'a','boot':refs['rescue']}},'highest_sequence':a.sequence,
             'system_blocked':True,'trial':None,'boot_attempt':None,'monitor_confirmed_boots':False,'bad_slots':[],
             'anchor_key_ids':[key_id],'next':{'target':'rescue','slot':'a','trial':False}})
        save(shared/'baselines/initial.json',{'release':RELEASE,'targets':['rescue','system']})
        save(shared/'baselines/current-rescue.json',{'release':RELEASE,'target':'rescue'})
        save(shared/'baselines/current-system.json',{'release':RELEASE,'target':'system'})
        factory=shared/'factory';factory.mkdir()
        run('tar','--clamp-mtime','--mtime=@'+str(EPOCH),'--zstd','--numeric-owner','--acls','--xattrs','--xattrs-include=*',
            '--one-file-system','--exclude=./boot/*','--exclude=./dev/*','--exclude=./proc/*','--exclude=./sys/*',
            '--exclude=./run/*','--exclude=./tmp/*','--exclude=./data/*','-cpf',factory/'factory-rootfs.tar.zst','-C',normal,'.')
        run('tar','--clamp-mtime','--mtime=@'+str(EPOCH),'--zstd','--numeric-owner','--acls','--xattrs','--xattrs-include=*',
            '-cpf',factory/'boot-files.tar.zst','-C',boot,'.')
        configured_target=json.loads((normal/'etc/image-framework/node.json').read_text())['target']
        record={'schema_version':1,'target':configured_target,'kernel_release':KERNEL,'authentication':'root_password',
                'boot_files':boot_hashes,'files':{}}
        for p in factory.glob('*.tar.zst'):record['files'][p.name]={'path':p.name,'size_bytes':p.stat().st_size,'sha256':sha(p)}
        save(factory/'manifest.json',record)
    # Build filesystems with hard-link deduplication of cache/baseline/kernel copies.
    shared_img=work/'shared.ext4'
    if not shared_img.exists():
        run('truncate','-s','1G',shared_img);run('mkfs.ext4','-F','-m','0','-N','32768','-J','size=16','-L','OTA_SHARED','-d',shared,shared_img)
        run('e2fsck','-f','-p',shared_img,accepted=(0,1));run('resize2fs','-M',shared_img)
    boot_img=work/('boot.fat' if amlogic else 'boot.ext4')
    if not boot_img.exists():
        run('truncate','-s','512M',boot_img)
        if amlogic:
            run('mkfs.vfat','-F','32','-n',board['labels']['boot'],boot_img)
            mounted=work/'boot-fat-mount';mounted.mkdir(exist_ok=True)
            run('mount','-o','loop',boot_img,mounted)
            try:run('cp','-r',str(boot)+'/.',mounted)
            finally:run('umount',mounted)
        else:run('mkfs.ext4','-F','-m','0','-L','OECT_BOOT','-U','ef67b6da-1f94-4a8f-9996-6ab6c6c88720','-d',boot,boot_img)
    shared_bytes=((shared_img.stat().st_size+128*1024**2+1024**2-1)//1024**2)*1024**2
    image_bytes=((board['shared_start']*512+shared_bytes+34*512+256*1024**2-1)//(256*1024**2))*(256*1024**2)
    image=output/(STEM+'.img');pending=output/(STEM+'.img.building')
    if not image.exists():
        with pending.open('xb') as f:f.truncate(image_bytes)
        if amlogic:
            import struct
            mbr=bytearray(512);struct.pack_into('<I',mbr,440,disk_signature)
            rows=[(board['boot_start'],board['boot_sectors'],0x0c),(board['rescue_a_start'],board['rescue_a_sectors'],0x83),
                  (board['rescue_b_start'],board['rescue_a_sectors'],0x83),(board['shared_start'],image_bytes//512-board['shared_start'],0x83)]
            for n,(start,length,kind) in enumerate(rows):
                entry=bytearray(16);entry[0]=0x80 if n==0 else 0;entry[1:4]=b'\xfe\xff\xff';entry[4]=kind;entry[5:8]=b'\xfe\xff\xff'
                struct.pack_into('<II',entry,8,start,length);mbr[446+16*n:462+16*n]=entry
            mbr[510:]=b'\x55\xaa'
            with pending.open('r+b') as f:f.write(mbr)
        else:run('sgdisk','--clear','--new=1:'+str(board['boot_start'])+':+'+str(board['boot_sectors']),
            '--typecode=1:8300','--change-name=1:OECT_BOOT','--attributes=1:set:2',
            '--partition-guid=1:cc0f1a32-bc60-49d5-9118-47095b77a41e',
            '--new=2:'+str(board['rescue_a_start'])+':+'+str(board['rescue_a_sectors']),
            '--typecode=2:8300','--change-name=2:OECT_RESCUE','--partition-guid=2:1f2a334c-eb25-4ab3-aadc-b38836beaa42',
            '--new=3:'+str(board['rescue_b_start'])+':+'+str(board['rescue_a_sectors']),
            '--typecode=3:8300','--change-name=3:ARMHPC_RESCUE_B',
            '--new=4:'+str(board['shared_start'])+':0','--typecode=4:8300','--change-name=4:OTA_SHARED',pending)
        with base.open('rb') as src,pending.open('r+b') as dst:
            first=512 if amlogic else 34*512
            src.seek(first);dst.seek(first);left=board['protected_prefix_bytes']-first
            while left:
                chunk=src.read(min(left,1024**2));assert chunk;dst.write(chunk);left-=len(chunk)
            for src_path,start in [(boot_img,board['boot_start']),(sq,board['rescue_a_start']),(sq,board['rescue_b_start']),(shared_img,board['shared_start'])]:
                dst.seek(start*512)
                with src_path.open('rb') as source:shutil.copyfileobj(source,dst,1024**2)
            dst.flush();os.fsync(dst.fileno())
        for start in (board['rescue_a_start'],board['rescue_b_start']):
            with pending.open('r+b') as f:
                from armhpc_ota.squashfs import trailer_bytes
                f.seek((start+board['rescue_a_sectors'])*512-TRAILER_BYTES);f.write(trailer_bytes(manifest_path.read_bytes()));f.flush();os.fsync(f.fileno())
        if amlogic:
            from framework.amlogic_image import inspect
            audit=inspect(pending,board,json.loads(a.base_provenance.read_text())['permanent_boot'])
            save(output/'IMAGE-AUDIT.json',audit)
        else:run('sgdisk','--verify',pending)
        pending.rename(image)
    save(output/'compression-comparison.json',comparison)
    meta={'schema_version':1,'release':RELEASE,'kernel_release':KERNEL,'work_directory':str(work),
          'source_versions':compatibility['source_versions'],'compatibility':compatibility,
          'image':{'path':str(image),'size_bytes':image.stat().st_size,'sha256':sha(image)},
          'layout':board,'rescue_filesystem':'squashfs','selected_compression':selected,
          'rescue_image_bytes':sq.stat().st_size,'rescue_slot_bytes':board['rescue_a_sectors']*512,
          'slot_growth_reserve_bytes':board['rescue_a_sectors']*512-TRAILER_BYTES-sq.stat().st_size,
          'shared_initial_filesystem_bytes':shared_img.stat().st_size,
          'shared_full_emmc_bytes':board['minimum_emmc_bytes']-board['shared_start']*512-board.get('disk_tail_reserved_sectors',33)*512,
          'shared_grows_on_first_boot':True,'rescue_ab_preinstalled':True,'bootstrap_required':False,
          'fixed_kernel_sha256':kernel_hash,'boot_files_sha256':boot_hashes,'dtb_sha256':sha(dtb),
          'hardware_verified':False,'kexec_watchdog_accepted':False,'hardware_policy':'development',
          'preserved_permanent_boot':json.loads(a.base_provenance.read_text())['permanent_boot']}
    save(output/'BUILD-RESULT.json',meta);print(json.dumps(meta,indent=2),flush=True)

if __name__=='__main__':main()
