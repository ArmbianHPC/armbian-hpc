#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Prepare a signed OTA transport with exact source archives and distribution license records."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def source_inputs(path):
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    rows = document.get('sources', [])
    if not rows or not any(row.get('kind') == 'kernel' for row in rows):
        raise ValueError('Include the corresponding retained kernel source')
    names = set()
    for row in rows:
        original = Path(row['path'])
        source = original.resolve(strict=True)
        name = row.get('name', source.name)
        if Path(name).name != name or name in names or not name.endswith(('.tar.xz', '.json')):
            raise ValueError('Unsafe or duplicate source archive name')
        if original.is_symlink() or not source.is_file() or sha(source) != row['sha256']:
            raise ValueError('Corresponding source input hash differs: ' + name)
        if name.endswith('.tar.xz'):
            with tarfile.open(source, 'r:xz') as archive:
                for member in archive:
                    parts = Path(member.name).parts
                    if member.name.startswith('/') or '..' in parts or member.name.endswith(('.pem', '.key', '.pfx', '.p12')):
                        raise ValueError('Unsafe corresponding source member')
        names.add(name)
    return rows


def archive_commit(repo, commit, destination):
    tar = subprocess.Popen(['git', '-C', str(repo), '-c', 'core.autocrlf=false', 'archive',
        '--format=tar', '--prefix=' + repo.name + '/', commit], stdout=subprocess.PIPE)
    with destination.open('xb') as stream:
        xz = subprocess.Popen(['xz', '-T4', '-6', '-c'], stdin=tar.stdout, stdout=stream)
        tar.stdout.close()
        if xz.wait() or tar.wait():
            raise ValueError('Corresponding project source archive failed')


def prepare(project, ota_source, manifest, artifacts, trust, roots, source_manifest,
            provenance, output):
    project, ota_source, artifacts, roots, output = (
        Path(p).resolve() for p in (project, ota_source, artifacts, roots, output))
    if output.exists() or output.is_relative_to(project) or output.is_relative_to(ota_source):
        raise ValueError('Use a fresh package staging directory outside repositories')
    refs = {}
    for repo in (project, ota_source):
        if subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain']).strip():
            raise ValueError('Package from clean committed project source')
        refs[repo.name] = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    sources = source_inputs(source_manifest)
    sys.path.insert(0, str(ota_source / 'src'))
    from armhpc_ota.metadata import verify, validate_manifest
    from armhpc_ota.local_package import inspect
    envelope = Path(manifest).read_bytes()
    payload = validate_manifest(verify(envelope, trust))
    for row in payload['artifacts']:
        path = artifacts / row['url'].rsplit('/', 1)[-1]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['size'] or sha(path) != row['sha256']:
            raise ValueError('Signed payload differs before packaging: ' + row['role'])
    output.mkdir(parents=True)
    release = output / 'releases' / payload['release']
    release.mkdir(parents=True)
    (release / 'manifest.json').write_bytes(envelope)
    blobs = output / 'blobs'
    blobs.mkdir()
    for row in payload['artifacts']:
        destination = blobs / row['sha256']
        if not destination.exists():
            shutil.copy2(artifacts / row['url'].rsplit('/', 1)[-1], destination)
    corresponding = output / 'corresponding-sources'
    corresponding.mkdir()
    for repo in (project, ota_source):
        archive_commit(repo, refs[repo.name], corresponding / (repo.name + '-' + refs[repo.name] + '.tar.xz'))
    records = []
    for row in sources:
        target = corresponding / row.get('name', Path(row['path']).name)
        shutil.copy2(row['path'], target)
        records.append({k: value for k, value in row.items() if k != 'path'})
    licenses = output / 'licenses'
    licenses.mkdir()
    inventory = {}
    for role in ('system', 'rescue'):
        root = roots / 'public' / role
        rows = subprocess.check_output(['chroot', str(root), 'dpkg-query', '-W',
            '-f=${binary:Package}\t${Version}\t${source:Package}\t${source:Version}\t${db:Status-Status}\n'], text=True)
        inventory[role] = [dict(zip(('package', 'version', 'source_package', 'source_version'), row.split('\t')[:4]))
            for row in rows.splitlines() if row.split('\t')[-1] == 'installed']
        destination = licenses / role
        destination.mkdir()
        for path in (root / 'usr/share/doc').glob('*/copyright'):
            if path.is_file():
                shutil.copyfile(path, destination / (path.parent.name + '.copyright'))
        firmware = root / 'usr/share/doc/armhpc-firmware'
        if firmware.is_dir():
            shutil.copytree(firmware, destination / 'board-firmware')
    (corresponding / 'distribution-packages.json').write_text(json.dumps(inventory, indent=2) + '\n')
    for repo in (project, ota_source):
        shutil.copytree(repo / 'LICENSES', licenses / repo.name)
        for name in ('LICENSE', 'COPYRIGHT', 'THIRD-PARTY-NOTICES.md', 'THIRD_PARTY_NOTICES.md'):
            source = repo / name
            if source.is_file():
                shutil.copy2(source, licenses / (repo.name + '-' + name))
        subprocess.run(['python3', str(repo / 'tools/license-audit.py'), '--output',
                        str(licenses / (repo.name + '-sources.json'))], check=True)
    record = json.loads(Path(provenance).read_text())
    record.update(source_commits=refs, corresponding_sources=records, board=payload['board'],
                  release=payload['release'], fixed_boot=json.loads((roots / 'boot-manifest.json').read_text()))
    (output / 'BUILD-SOURCES.json').write_text(json.dumps(record, indent=2) + '\n')
    (output / 'README.md').write_text('# ARMHPC system OTA\n\n[简体中文](README.zh-CN.md)\n\n'
        'Import this signed archive into the matching ARMHPC OTA controller. Select system, Rescue or all. '
        'Update older agents with the matching maintenance package first.\n\n'
        'The archive includes signed payloads, project and kernel sources, retained BOOT integration sources, '
        'distribution package versions and license materials. BUILD-SOURCES.json identifies the exact inputs.\n')
    (output / 'README.zh-CN.md').write_text('# ARMHPC 系统 OTA\n\n'
        '在配套 ARMHPC OTA 上位机导入本签名包，选择 system、rescue 或 all。旧 agent 先安装配套维护包。\n\n'
        '本包包含签名载荷、项目与内核源码、保留 BOOT 的接入源码、发行版软件版本和许可材料。'
        'BUILD-SOURCES.json 记录各项输入。\n')
    (output / 'SHA256SUMS').write_text(''.join(sha(p) + '  ' + p.relative_to(output).as_posix() + '\n'
        for p in sorted(output.rglob('*')) if p.is_file()))
    result = inspect(output, output.parent / (output.name + '-inspection'))
    return {'release': payload['release'], 'source_commits': refs, 'package': str(output), 'inspection': result['release']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('project', 'ota-source', 'manifest', 'artifacts', 'trust', 'roots',
                 'source-manifest', 'provenance', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    print(json.dumps(prepare(**vars(parser.parse_args())), indent=2))
