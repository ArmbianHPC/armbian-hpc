#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Package paired firmware and a canonical signed archive for the OTA controller."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def package(project, ota_source, package_root, image, output, trust, stem, verify_only=False,
            reuse_compressed_image=False):
    project, ota_source, package_root, image, output, trust = (
        Path(p).resolve() for p in (project, ota_source, package_root, image, output, trust))
    if not re.fullmatch(r'armhpc_\d+\.\d+\.\d+_[a-zA-Z0-9.-]+_[a-zA-Z0-9.-]+_\d+\.\d+\.\d+_[a-zA-Z0-9.-]+_\d{8}', stem):
        raise ValueError('Use the version/model/distribution/kernel/filesystem/date firmware stem')
    if output.is_relative_to(project) or output.is_relative_to(ota_source) or output.is_relative_to(package_root):
        raise ValueError('Build outside the source repositories and input staging tree')
    if not image.is_file() or image.is_symlink() or image.name != stem + '.img':
        raise ValueError('Raw image name must match the requested firmware stem')
    for name in ('blobs', 'releases', 'corresponding-sources', 'licenses'):
        if not (package_root / name).is_dir() or (package_root / name).is_symlink():
            raise ValueError('Missing prepared package resource: ' + name)
    for path in package_root.rglob('*'):
        if path.is_symlink():
            raise ValueError('Package staging must not contain symbolic links')
        if path.suffix.lower() in ('.pem', '.key', '.pfx', '.p12'):
            raise ValueError('Private key or credential resource in package staging')
    sys.path.insert(0, str(ota_source / 'src'))
    from armhpc_ota.local_package import inspect
    from armhpc_ota.metadata import verify, validate_manifest
    info = inspect(package_root, output / 'inspection')
    manifest = validate_manifest(verify(Path(info['manifest']).read_bytes(), trust))
    required = {target + '-' + role for target in ('system', 'rescue') for role in ('rootfs', 'kernel', 'initramfs', 'dtb')}
    if {a['role'] for a in manifest['artifacts']} != required:
        raise ValueError('The firmware package must contain paired system and Rescue payloads')
    compressed = output / (stem + '.img.xz')
    archive = output / (stem.replace('armhpc_', 'armhpc-ota_', 1) + '.tar.xz')
    output.mkdir(parents=True, exist_ok=True)
    if not verify_only:
        if (compressed.exists() and not reuse_compressed_image) or archive.exists():
            raise ValueError('Versioned package already exists; do not overwrite it')
        if reuse_compressed_image and (not compressed.is_file() or compressed.is_symlink()):
            raise ValueError('Provide the already compressed native image for full verification')
        if os.name == 'nt':
            raise ValueError('Run firmware compression on Linux or WSL')
        sys.path.insert(0, str(project))
        from framework.artifacts import package_image
        if not reuse_compressed_image:
            if image.parent != output:
                raise ValueError('Place the image in the output directory before compression')
            package_image(image, expected_sha256=sha256(image), threads=4)
        pending = archive.with_name(archive.name + '.building')
        with pending.open('xb') as destination:
            tar = subprocess.Popen(['tar', '--sort=name', '--numeric-owner', '-cf', '-', '-C', str(package_root),
                                    *(p.name for p in sorted(package_root.iterdir()))], stdout=subprocess.PIPE)
            xz = subprocess.Popen(['xz', '-T4', '-6', '-c'], stdin=tar.stdout, stdout=destination)
            tar.stdout.close()
            if xz.wait() or tar.wait():
                raise ValueError('OTA archive compression failed')
            destination.flush()
            os.fsync(destination.fileno())
        pending.replace(archive)
    if not compressed.is_file() or not archive.is_file():
        raise ValueError('Both XZ packages are required')
    import lzma
    digest, count = hashlib.sha256(), 0
    with lzma.open(compressed, 'rb') as source:
        for chunk in iter(lambda: source.read(4 * 1024**2), b''):
            digest.update(chunk)
            count += len(chunk)
    if count != image.stat().st_size or digest.hexdigest() != sha256(image):
        raise ValueError('Compressed image does not match the raw image')
    packaged = inspect(archive, output / 'controller-inspection')
    packaged_manifest = validate_manifest(verify(Path(packaged['manifest']).read_bytes(), trust))
    if packaged_manifest != manifest:
        raise ValueError('Packaged manifest differs from the staging input')
    return {'release': info['release'], 'signature_verified': True, 'payloads': len(manifest['artifacts']),
            'files': [{'file': p.name, 'bytes': p.stat().st_size, 'sha256': sha256(p)} for p in (compressed, archive)]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('project', 'ota-source', 'package-root', 'image', 'output', 'trust'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--stem', required=True)
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--reuse-compressed-image', action='store_true')
    print(json.dumps(package(**vars(parser.parse_args())), indent=2))
