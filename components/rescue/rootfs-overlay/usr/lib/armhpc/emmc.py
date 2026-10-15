#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Explicit OESP commissioning backup and signed offline image installation."""
import argparse, hashlib, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from config_io import save_json, require, approve, emit, entry

def command(*args):
    return subprocess.check_output(list(map(str,args)),text=True).strip()

def digest(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def device():
    require(os.geteuid()==0 and sys.platform=='linux','platform','Run as root in Rescue')
    require(Path('/etc/armhpc/rescue-environment').is_file() and Path('/run/armhpc-install-medium').exists(),
            'commissioning','eMMC installation requires the USB commissioning environment')
    compatible=Path('/proc/device-tree/compatible').read_bytes().split(b'\0')
    require(b'armhpc,oes1plus-v22' in compatible,'board_mismatch','This installer is for OES1PLUS v2.2')
    nodes=[]
    for path in Path('/sys/class/block').glob('mmcblk[0-9]*'):
        if 'boot' in path.name or 'p' in path.name:continue
        kind=path/'device/type';cid=path/'device/cid'
        if kind.is_file() and kind.read_text().strip()=='MMC' and cid.is_file():nodes.append((Path('/dev')/path.name,cid.read_text().strip()))
    require(len(nodes)==1,'emmc_identity','A unique eMMC device is required')
    disk,cid=nodes[0];info=json.loads(command('lsblk','--json','--bytes','--tree','-o','NAME,PATH,SIZE,TYPE,MOUNTPOINTS',disk))['blockdevices'][0]
    def unmounted(row):
        require(not any(row.get('mountpoints') or []),'mounted','eMMC or a child partition is mounted')
        for child in row.get('children',[]):unmounted(child)
    unmounted(info)
    return disk,cid,int(info['size'])

def backup(directory):
    disk,cid,size=device();directory=Path(directory).resolve(strict=True)
    require(directory.is_dir() and not directory.is_symlink(),'backup_directory','Choose a mounted external backup directory')
    source=command('findmnt','-rn','-T',directory,'-o','SOURCE')
    if source.startswith('/dev/'):
        chain=command('lsblk','-sn','-p','-o','PATH',source).split()
        require(str(disk) not in chain,'backup_directory','Backup cannot reside on this eMMC')
    require(shutil.disk_usage(directory).free>=size+64*1024**2,'backup_space','Backup needs eMMC capacity plus 64 MiB free')
    owner=hashlib.sha256(cid.encode()).hexdigest();target=directory/('armhpc-emmc-backup-'+owner[:12])
    target.mkdir(mode=0o700,exist_ok=True);rows=[]
    for suffix,bytes_count in [('',size),('boot0',None),('boot1',None)]:
        block=Path(str(disk)+suffix);count=bytes_count or int(command('blockdev','--getsize64',block))
        destination=target/('user.img' if not suffix else suffix+'.img')
        require(not destination.exists(),'backup_exists','Existing backup is never overwritten')
        h=hashlib.sha256();left=count
        with block.open('rb') as src,destination.open('xb') as out:
            destination.chmod(0o600)
            while left:
                chunk=src.read(min(left,4*1024**2));require(chunk,'backup_read','Short eMMC read')
                out.write(chunk);h.update(chunk);left-=len(chunk)
            out.flush();os.fsync(out.fileno())
        require(digest(destination)==h.hexdigest(),'backup_hash','Backup readback differs')
        rows.append({'file':destination.name,'bytes':count,'sha256':h.hexdigest()})
    data={'protocol':1,'kind':'emmc-backup','board':'wxy-oesp-v22','cid_sha256':owner,'device_bytes':size,'files':rows}
    save_json(target/'backup.json',data)
    return {'directory':str(target),'device_bytes':size,'files':rows,'complete':True}

def install(image, manifest, receipt, execute=False, yes=False):
    disk,cid,size=device();sys.path.insert(0,'/usr/lib/armhpc-ota')
    from armhpc_ota.config import Config
    from armhpc_ota.metadata import verify
    from armhpc_ota.io import writer_lock
    config=Config.load();image=Path(image).resolve(strict=True)
    signed=verify(Path(manifest).read_bytes(),config.trust)
    require(signed.get('kind')=='image' and signed.get('board')=='wxy-oesp-v22' and signed.get('architecture')=='arm64'
            and signed.get('layout')=='oesp-mbr-squashfs-ab-v1','image_manifest','Wrong signed image metadata')
    require(image.stat().st_size==signed['image']['bytes'] and digest(image)==signed['image']['sha256'],
            'image_hash','Image differs from its signed manifest')
    require(image.stat().st_size<=size and size>=7516192768,'emmc_capacity','Image does not fit the eMMC')
    receipt=Path(receipt).resolve(strict=True);old=json.loads(receipt.read_text())
    require(old.get('kind')=='emmc-backup' and old.get('cid_sha256')==hashlib.sha256(cid.encode()).hexdigest()
            and old.get('device_bytes')==size,'backup_identity','Backup belongs to another eMMC')
    require({r['file'] for r in old['files']}=={'user.img','boot0.img','boot1.img'},'backup_manifest','Incomplete backup inventory')
    for row in old['files']:
        source=receipt.parent/row['file'];require(source.is_file() and not source.is_symlink()
            and source.stat().st_size==row['bytes'] and digest(source)==row['sha256'],'backup_hash','Backup resource failed verification')
    result={'device':str(disk),'image_bytes':image.stat().st_size,'backup_verified':True,'execute':execute}
    if not execute:return result
    approve(yes,'写入完整 OESP eMMC 镜像？原厂系统将被替换；SATA 分区不在本次写入范围。')
    # Keep this USB instance's device-owned identities for installation into the new shared partition.
    identity=Path('/var/lib/armhpc/ota/identity-root')
    with tempfile.TemporaryDirectory(prefix='armhpc-emmc-identity-',dir='/run') as temp:
        retained=Path(temp)/'identity-root'
        if identity.is_dir():shutil.copytree(identity,retained,symlinks=True)
        with writer_lock(config.global_writer_lock):
            disk2,cid2,size2=device();require((disk,cid,size)==(disk2,cid2,size2),'emmc_identity','eMMC changed before writing')
            with image.open('rb') as src,disk.open('r+b',buffering=0) as out:
                while data:=src.read(4*1024**2):out.write(data)
                os.fsync(out.fileno())
            with disk.open('rb') as source:
                h=hashlib.sha256();left=image.stat().st_size
                while left:
                    data=source.read(min(left,4*1024**2));require(data,'image_readback','Short image readback');h.update(data);left-=len(data)
            require(h.hexdigest()==signed['image']['sha256'],'image_readback','eMMC readback differs')
            subprocess.run(['partx','--update',str(disk)],check=True);subprocess.run(['udevadm','settle'],check=True)
            shared=Path(str(disk)+'p4');mount=Path(temp)/'shared';mount.mkdir()
            subprocess.run(['mount','-t','ext4',str(shared),str(mount)],check=True)
            try:
                if retained.is_dir():shutil.copytree(retained,mount/'identity-root',dirs_exist_ok=True,symlinks=True)
                from armhpc_ota.backend import Backend
                installed=Config(profile='wxy-oesp-v22',state=mount,storage_layout='oesp-mbr-squashfs-ab-v1')
                Backend(installed).save_shared_identity(Path('/'),'rescue')
                for relative in ('clock','identity.json'):
                    source=config.state/relative;destination=mount/relative
                    if source.is_dir():shutil.copytree(source,destination,dirs_exist_ok=True)
                    elif source.is_file():shutil.copy2(source,destination)
                subprocess.run(['sync','-f',str(mount)],check=True)
            finally:subprocess.run(['umount',str(mount)],check=True)
    return {**result,'complete':True,'cold_start_required':True}

def main(argv=None):
    p=argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG'),description=__doc__)
    p.add_argument('action',choices=('backup','install'));p.add_argument('--directory');p.add_argument('--image');p.add_argument('--manifest');p.add_argument('--backup')
    p.add_argument('--execute',action='store_true');p.add_argument('--yes',action='store_true');p.add_argument('--json',action='store_true');a=p.parse_args(argv)
    if a.action=='backup':
        require(a.directory,'arguments','Provide --directory on mounted external storage')
        approve(a.yes,'创建本机 eMMC 私有备份？');result=backup(a.directory)
    else:
        require(a.image and a.manifest and a.backup,'arguments','Provide --image, --manifest and --backup backup.json')
        result=install(a.image,a.manifest,a.backup,a.execute,a.yes)
    emit(result,a.json)

if __name__=='__main__':entry(main)
