# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Private registry configuration with explicit fallback and redacted status."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import urllib.parse
from config_io import (approve,atomic_write,configuration_lock,emit,entry,normal_system,
                       read_json,require,require_idle,safe_path,save_json)
from node_state import data_mount


def validate(text,allow_http=False):
    from kube_runtime import check_configuration
    from kube_setup import validate_registry
    value=check_configuration(text)
    require(not set(value)-{'mirrors','configs'},'registry_schema','Unsupported registry configuration key')
    require(isinstance(value.get('mirrors',{}),dict) and isinstance(value.get('configs',{}),dict),
            'registry_schema','Mirrors and configs must be mappings')
    for name,item in value.get('mirrors',{}).items():
        if name!='*':validate_registry(name)
        require(isinstance(item,dict) and not set(item)-{'endpoint','rewrite'} and isinstance(item.get('endpoint'),list),
                'registry_mirror','Each mirror needs explicit endpoint URLs')
        for url in item['endpoint']:
            parsed=urllib.parse.urlsplit(url)
            require(parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                    and (parsed.scheme=='https' or allow_http and parsed.scheme=='http'),
                    'registry_tls','Use HTTPS registry endpoints, or explicitly allow HTTP')
    for name,item in value.get('configs',{}).items():
        if name!='*':validate_registry(name)
        require(isinstance(item,dict) and not set(item)-{'auth','tls'},'registry_schema','Unknown registry authentication/TLS option')
        auth=item.get('auth',{});tls=item.get('tls',{})
        require(isinstance(auth,dict) and not set(auth)-{'username','password','auth','identitytoken'},'registry_auth','Unsupported registry authentication option')
        require(isinstance(tls,dict) and not set(tls)-{'ca_file','cert_file','key_file','insecure_skip_verify'},'registry_tls','Unsupported TLS option')
        require(not tls.get('insecure_skip_verify'),'registry_tls','TLS verification must remain enabled')
        require(bool(tls.get('cert_file'))==bool(tls.get('key_file')),'registry_tls','Supply both a client certificate and its private key')
    return value


def summary(value):
    return {'mirrors':value.get('mirrors',{}),'registries':{
        name:{'authentication_configured':bool(item.get('auth')),
              'tls_files':[k for k in item.get('tls',{}) if k.endswith('_file')]}
        for name,item in value.get('configs',{}).items()}}


def apply(policy,value,fallback):
    data_mount()
    for unit in ('k3s.service','k3s-agent.service'):
        require(subprocess.run(['systemctl','is-active','--quiet',unit],capture_output=True,timeout=5).returncode!=0,
                'registry_active','Drain and stop K3s before changing registry policy')
    folder=Path(policy['configuration_dir'])
    require(folder.is_relative_to('/data/node-config'),'registry_path','Registry resources must use the verified DATA configuration directory')
    folder=safe_path(Path('/'),str(folder.relative_to('/')))
    previous=read_json(Path(policy['state_file']),{})
    if previous.get('status')=='installed':
        from kube_runtime import verify_recovery
        verify_recovery()
    files=[]
    for item in value.get('configs',{}).values():
        tls=item.get('tls',{})
        for name in ('ca_file','cert_file','key_file'):
            if not tls.get(name):continue
            source=Path(tls[name])
            require(source.is_absolute() and not source.is_symlink() and source.is_file() and source.stat().st_uid==0
                    and source.stat().st_size<=1024**2 and not source.stat().st_mode&0o022,
                    'registry_file','TLS files must be bounded, root-owned regular files')
            content=source.read_bytes();digest=hashlib.sha256(content).hexdigest()
            destination=folder/'registry-files'/(digest+'.pem')
            if name=='ca_file':ssl.create_default_context(cafile=source)
            files.append((destination,content));tls[name]=str(destination)
        if tls.get('key_file'):
            from cryptography import x509
            from cryptography.hazmat.primitives.serialization import load_pem_private_key,Encoding,PublicFormat
            contents=dict(files)
            key=load_pem_private_key(contents[Path(tls['key_file'])],password=None)
            cert=x509.load_pem_x509_certificate(contents[Path(tls['cert_file'])])
            require(key.public_key().public_bytes(Encoding.DER,PublicFormat.SubjectPublicKeyInfo)==
                    cert.public_key().public_bytes(Encoding.DER,PublicFormat.SubjectPublicKeyInfo),
                    'registry_tls','Client certificate does not match its private key')
    config=folder/'registries.yaml';raw=json.dumps(value,indent=2)+'\n'
    for destination,content in files:atomic_write(safe_path(Path('/'),str(destination.relative_to('/'))),content,0o600)
    if config.exists():atomic_write(config.with_name('registries.previous.yaml'),config.read_bytes(),0o600)
    atomic_write(config,raw,0o600)
    save_json(folder/'registry-managed.json',{'schema_version':1,'sha256':hashlib.sha256(raw.encode()).hexdigest(),
        'default_fallback':fallback,'allow_http':any(url.startswith('http:') for item in value.get('mirrors',{}).values() for url in item['endpoint'])})
    current=folder/'config.yaml'
    if current.exists():
        from kube_runtime import check_configuration,refresh_recovery
        options=check_configuration(current.read_text())
        options['disable-default-registry-endpoint']=fallback=='deny';options['private-registry']=str(config)
        atomic_write(current,'\n'.join(k+': '+json.dumps(v) for k,v in options.items())+'\n',0o600)
        if previous.get('status')=='installed':
            refresh_recovery(changed=[str(p.relative_to('/')) for p in
                (current,config,folder/'registry-managed.json',*(p for p,_ in files))])
    return {**summary(value),'default_fallback':fallback,'applied':True,
            'fallback_scope':'only registries configured in mirrors; external firewall rules remain independent',
            'activation':'next K3s start'}


def main():
    parser=argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG','armhpc-kube registry'))
    parser.add_argument('action',nargs='?',default='status',choices=('status','import'))
    parser.add_argument('--file',type=Path);parser.add_argument('--default-fallback',choices=('allow','deny'))
    parser.add_argument('--allow-http',action='store_true');parser.add_argument('--apply',action='store_true')
    parser.add_argument('--yes',action='store_true');parser.add_argument('--json',action='store_true')
    args=parser.parse_args();normal_system(Path('/'));data_mount()
    policy=read_json(Path('/etc/image-framework/k3s.json'))
    path=Path(policy['configuration_dir'])/'registries.yaml'
    if args.action=='status':
        managed=read_json(path.with_name('registry-managed.json'),{})
        result={**(summary(validate(path.read_text(),managed.get('allow_http',False))) if path.is_file() else {}),
                'managed':bool(managed),'default_fallback':managed.get('default_fallback','from K3s configuration')}
    else:
        require(args.file is not None and args.default_fallback is not None,'registry_arguments','Supply --file and --default-fallback')
        require(not args.file.is_symlink() and args.file.is_file() and args.file.stat().st_size<=1024**2
                and args.file.stat().st_uid==0 and not args.file.stat().st_mode&0o077,'registry_file','Import a root-owned mode 0600 registry file')
        value=validate(args.file.read_text(),args.allow_http)
        result={**summary(value),'default_fallback':args.default_fallback,'applied':False}
        if args.apply:
            with configuration_lock(Path('/')):
                require_idle(Path('/'));approve(args.yes,'Apply the selected private registry configuration?')
                result=apply(policy,value,args.default_fallback)
    emit(result,args.json)


if __name__=='__main__':raise SystemExit(entry(main))
