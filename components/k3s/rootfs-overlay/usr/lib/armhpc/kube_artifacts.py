# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Version-bound K3s program and airgap archives, retained on verified DATA."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import tempfile
import urllib.parse
import urllib.request
from config_io import atomic_write,require,run,safe_path,save_json,sync_directory
from node_state import data_mount,digest


def sums(text):
    result={}
    for line in text.splitlines():
        match=re.fullmatch(r'([0-9a-fA-F]{64})\s+\*?([^/\\\s]+)',line.strip())
        if match:
            require(match[2] not in result,'k3s_checksum','Duplicate release checksum entry')
            result[match[2]]=match[1].lower()
    require(result,'k3s_checksum','Release has no valid SHA256 entries')
    return result


def fetch(url,path,expected=None,maximum=2*1024**3,ca=None):
    path=Path(path)
    if path.is_file() and expected and digest(path)==expected:return
    require(urllib.parse.urlsplit(url).scheme=='https','k3s_https','Release resources require HTTPS')
    path.parent.mkdir(parents=True,exist_ok=True)
    context=ssl.create_default_context(cafile=ca)
    fd,temporary=tempfile.mkstemp(prefix='.download-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as destination,urllib.request.urlopen(
                urllib.request.Request(url,headers={'User-Agent':'ARMHPC/1.3'}),context=context,timeout=30) as response:
            require(urllib.parse.urlsplit(response.geturl()).scheme=='https','k3s_https','HTTPS downgrade refused')
            size=int(response.headers.get('Content-Length','0'))
            require(size<=maximum and shutil.disk_usage(path.parent).free>=size+512*1024**2,
                    'k3s_space','Release resource exceeds size or available DATA capacity')
            count=0;checksum=hashlib.sha256()
            for chunk in iter(lambda:response.read(1024**2),b''):
                count+=len(chunk);require(count<=maximum,'k3s_size','Release resource exceeds its limit')
                checksum.update(chunk);destination.write(chunk)
            require(not size or count==size,'k3s_download','Truncated release resource')
            require(not expected or checksum.hexdigest()==expected,'k3s_checksum','Downloaded release SHA256 differs')
            destination.flush();os.fsync(destination.fileno())
        os.replace(temporary,path);path.chmod(0o600);sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def copy(source,target,mode=0o600):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    require(not target.is_symlink(),'k3s_path','Recovery destination cannot be a symbolic link')
    fd,temporary=tempfile.mkstemp(prefix='.resource-',dir=target.parent)
    try:
        os.fchmod(fd,mode)
        with os.fdopen(fd,'wb') as out,Path(source).open('rb') as src:
            shutil.copyfileobj(src,out,1024**2);out.flush();os.fsync(out.fileno())
        os.replace(temporary,target);sync_directory(target.parent)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def release_prefix(source,version):
    base=source['artifact_url'].rstrip('/')
    release=version.replace('+','-') if base=='https://rancher-mirror.rancher.cn/k3s' else version
    return base+'/'+urllib.parse.quote(release,safe='')+'/'


def prepare(policy,plan,installer,installer_sha256):
    data,identity=data_mount()
    version=plan['version'];arch=policy['installer_arch']
    require(re.fullmatch(r'v\d+\.\d+\.\d+\+k3s\d+',version) and arch in ('arm64','arm'),
            'k3s_version','Select a complete K3s version and supported architecture')
    folder=safe_path(data,'node-config/armhpc/k3s-artifacts/'+version+'/'+arch)
    folder.mkdir(parents=True,exist_ok=True)
    require(digest(installer)==installer_sha256,'k3s_installer','Installer differs from its pinned checksum')
    copy(installer,folder/'install.sh')
    checksum_file=folder/('sha256sum-'+arch+'.txt')
    source=plan['download_source'];offline=source.get('offline_directory')
    prefix=release_prefix(source,version)
    if offline:
        original=Path(offline)
        require(original.is_dir() and not original.is_symlink(),'k3s_offline','Provide an explicit local release directory')
        copy(original/checksum_file.name,checksum_file)
    elif not checksum_file.is_file():fetch(prefix+checksum_file.name,checksum_file,maximum=2*1024**2,ca=source.get('ca_file'))
    checksums=sums(checksum_file.read_text())
    program='k3s-arm64' if arch=='arm64' else 'k3s-armhf'
    airgap='k3s-airgap-images-'+arch+'.tar.zst'
    for name in (program,airgap):
        require(name in checksums,'k3s_checksum','Exact program/airgap asset is absent from the selected release checksum file: '+name)
        destination=safe_path(folder,name)
        if offline:
            require(digest(original/name)==checksums[name],'k3s_checksum','Local release resource checksum differs')
            if not destination.is_file() or digest(destination)!=checksums[name]:copy(original/name,destination)
        else:fetch(prefix+name,destination,checksums[name],ca=source.get('ca_file'))
    with (folder/program).open('rb') as stream:header=stream.read(20)
    require(header[:4]==b'\x7fELF' and int.from_bytes(header[18:20],'little')==(183 if arch=='arm64' else 40),
            'k3s_architecture','Program ELF architecture differs from the target')
    images=Path(policy['data_dir'])/'agent/images'/airgap
    copy(folder/airgap,images)
    copy(folder/program,Path('/usr/local/bin/k3s'),0o755)
    version_output=run('/usr/local/bin/k3s','--version',timeout=15)
    require(('k3s version '+version+' ') in version_output+' ','k3s_version','Program version differs from the chosen cluster version')
    receipt={'schema_version':1,'version':version,'architecture':arch,'data':identity,
             'source':source['artifact_url'],'checksum_file_sha256':digest(checksum_file),
             'installer_sha256':installer_sha256,'program_sha256':checksums[program],
             'airgap_sha256':checksums[airgap],'program':program,'airgap':airgap}
    save_json(folder/'manifest.json',receipt)
    return receipt
