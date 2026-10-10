#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Install a checksum-pinned Debian clock package into an isolated build root."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--package',type=Path,required=True)
    args=parser.parse_args()
    root=args.root.resolve(strict=True); package=args.package.resolve(strict=True)
    if os.geteuid()!=0 or root == Path('/') or not (root/'etc/debian_version').is_file() or any((p/'.git').exists() for p in (root,*root.parents)):
        raise RuntimeError('Use a new isolated Linux build root as root')
    manifest=json.loads((package.parent/'package.json').read_text())
    if hashlib.sha256(package.read_bytes()).hexdigest()!=manifest['sha256']:
        raise RuntimeError('Clock package checksum mismatch')
    architecture=subprocess.check_output(['chroot',str(root),'dpkg','--print-architecture'],text=True).strip()
    if architecture!=manifest['architecture']:
        raise RuntimeError('Clock package architecture differs from rootfs')
    policy=root/'usr/sbin/policy-rc.d'
    if policy.is_symlink(): raise RuntimeError('Build policy-rc.d cannot be a symlink')
    old=policy.read_bytes() if policy.exists() else None
    old_mode=policy.stat().st_mode & 0o777 if policy.exists() else None
    target=root/'tmp/armhpc-clock-package.deb'
    shutil.copyfile(package,target)
    policy.write_text('#!/bin/sh\nexit 101\n');policy.chmod(0o755)
    environment=os.environ.copy();environment['DEBIAN_FRONTEND']='noninteractive'
    try:
        subprocess.run(['chroot',str(root),'dpkg','--install','/tmp/armhpc-clock-package.deb'],check=True,env=environment)
        version=subprocess.check_output(['chroot',str(root),'dpkg-query','-W','-f=${Version}','chrony'],text=True).strip()
        if version!=manifest['version']: raise RuntimeError('Installed chrony version differs from pin')
    finally:
        target.unlink(missing_ok=True)
        if old is None: policy.unlink()
        else: policy.write_bytes(old);policy.chmod(old_mode)
    record=root/'usr/share/doc/armhpc-clock/package.json'
    record.parent.mkdir(parents=True,exist_ok=True)
    record.write_text(json.dumps({k:manifest[k] for k in ('package','version','architecture','sha256','source_url')},indent=2)+'\n')


if __name__=='__main__': main()
