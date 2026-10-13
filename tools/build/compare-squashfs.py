#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Measure compressors against the exact same sanitized Rescue staging tree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

def digest(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def fingerprint(root,ota_source):
    try:
        library=Path(ota_source).resolve(strict=True)/'src'
    except OSError as error:
        raise ValueError('The --ota-source checkout does not exist') from error
    module_path=library/'armhpc_ota/squashfs.py'
    if not module_path.is_file() or not module_path.resolve().is_relative_to(library):
        raise ValueError('The --ota-source checkout must contain src/armhpc_ota/squashfs.py')
    sys.path.insert(0,str(library))
    from armhpc_ota import squashfs
    if Path(squashfs.__file__).resolve()!=module_path.resolve():
        raise ValueError('A different OTA package is already loaded; run the comparison in a fresh process')
    return squashfs.staging_fingerprint(root)


def main():
    a=argparse.ArgumentParser();a.add_argument('--root',type=Path,required=True);a.add_argument('--output',type=Path,required=True)
    a.add_argument('--epoch',type=int,required=True);a.add_argument('--ota-source',type=Path,required=True,help='Matching armhpc-ota source checkout.');a.add_argument('--only',choices=('xz-1m','xz-256k','zstd22-1m','zstd19-1m','lz4hc-1m','gzip9-1m'));args=a.parse_args()
    root=args.root.resolve(strict=True);out=args.output.resolve()
    assert root != Path('/') and not any((p/'.git').exists() for p in (out,*out.parents))
    try:before=fingerprint(root,args.ota_source)
    except (ValueError,ImportError,OSError) as error:a.error(str(error))
    out.mkdir(parents=True,exist_ok=True)
    candidates=[('xz-1m',['-comp','xz','-b','1M','-Xdict-size','100%']),
                ('zstd22-1m',['-comp','zstd','-b','1M','-Xcompression-level','22']),
                ('zstd19-1m',['-comp','zstd','-b','1M','-Xcompression-level','19']),
                ('xz-256k',['-comp','xz','-b','256K','-Xdict-size','100%']),
                ('lz4hc-1m',['-comp','lz4','-b','1M','-Xhc']),
                ('gzip9-1m',['-comp','gzip','-b','1M','-Xcompression-level','9'])]
    if args.only:candidates=[item for item in candidates if item[0]==args.only]
    results=[]
    for name,options in candidates:
        image=out/(name+'.squashfs');record=out/(name+'.json')
        if record.exists():
            item=json.loads(record.read_text());assert item['source_fingerprint']==before and digest(image)==item['sha256'];results.append(item);continue
        assert not image.exists(), 'An incomplete compressor output must be inspected before resuming'
        command=['mksquashfs',str(root),str(image),'-noappend','-no-progress','-processors','4','-mem','1G','-mkfs-time',str(args.epoch),'-all-time',str(args.epoch),*options]
        print('Compressing '+name+'...',flush=True);start=time.monotonic()
        with (out/(name+'.log')).open('w') as log:subprocess.run(command,check=True,stdout=log,stderr=subprocess.STDOUT)
        item={'name':name,'options':options,'bytes':image.stat().st_size,'build_seconds':round(time.monotonic()-start,3),'sha256':digest(image),'source_fingerprint':before}
        record.write_text(json.dumps(item,indent=2)+'\n');results.append(item)
        print(json.dumps(item),flush=True)
    assert fingerprint(root,args.ota_source)==before,'Source changed during the comparison'
    results.sort(key=lambda v:v['bytes'])
    result={'scope':'native Linux build; runtime performance not measured','root':str(root),'source_fingerprint':before,
            'source_apparent_bytes':int(subprocess.check_output(['du','-sx','--apparent-size','--block-size=1',str(root)],text=True).split()[0]),
            'mksquashfs_version':subprocess.check_output(['mksquashfs','-version'],text=True).splitlines()[0],
            'results':results,'selected':results[0],'hardware_verified':False}
    (out/'compression-comparison.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2),flush=True)

if __name__=='__main__':main()
