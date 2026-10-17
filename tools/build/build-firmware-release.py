#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Build a paired firmware release from an explicit, private build-input JSON."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def build(config):
    if os.name == 'nt' or os.geteuid() != 0:
        raise ValueError('Firmware assembly requires Linux root')
    cfg = json.loads(Path(config).read_text())
    project = Path(__file__).resolve().parents[2]
    ota = Path(cfg['ota_source']).resolve(strict=True)
    version = (project / 'VERSION').read_text().strip()
    revision_file = project / 'REVISION'
    revision = revision_file.read_text().strip() if revision_file.exists() else None
    if revision and not re.fullmatch(r'r[1-9][0-9]*', revision):
        raise ValueError('Firmware revision must use lowercase r and a positive integer')
    display_version = version + ('-' + revision if revision else '')
    sys.path.insert(0, str(project))
    from framework.compatibility import check_sources
    compatibility = check_sources(ota, project)
    source_versions = compatibility['source_versions']
    for repo in (project, ota):
        if subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain']).strip():
            raise ValueError('Build from clean committed sources')
    output = Path(cfg['output']).resolve()
    if output.exists() or any((p / '.git').exists() for p in (output, *output.parents)):
        raise ValueError('Use a fresh output directory outside repositories')
    output.mkdir(parents=True)
    work = Path(cfg['work']).resolve()
    tools = project / 'tools/build'

    def run(script, *arguments):
        print('Running ' + script, flush=True)
        with (output / (script + '.log')).open('w') as log:
            subprocess.run(['python3', str(tools / script), *map(str, arguments)], check=True,
                           stdout=log, stderr=subprocess.STDOUT)

    run('prepare-firmware-inputs.py', '--project', project)
    prepare='rebase-kernel-roots.py' if cfg.get('kernel_work') else 'refresh-fixed-boot-roots.py'
    kernel_arguments=['--kernel-work',cfg['kernel_work']] if cfg.get('kernel_work') else []
    if cfg.get('resume_roots'):
        if not cfg.get('kernel_work'):raise ValueError('Only rebuilt-kernel roots support guarded resume')
        kernel_arguments.append('--resume')
    if cfg.get('apt_proxy'):kernel_arguments+=['--apt-proxy',cfg['apt_proxy']]
    if cfg.get('offline_dependencies'):
        if not cfg.get('kernel_work'):raise ValueError('Offline dependency verification requires the kernel-root rebase entry')
        kernel_arguments.append('--offline-dependencies')
    run(prepare, '--project', project, '--ota-source', ota,
        '--input-roots', cfg['input_roots'], '--boot-manifest', cfg['boot_manifest'],
        '--work', work, '--watchdog-binary', cfg['watchdog_binary'], '--board', cfg['board'],
        '--release', cfg['release'], '--epoch', cfg['epoch'],*kernel_arguments)
    provenance = json.loads(Path(cfg['provenance']).read_text())
    refs = {repo.name: subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            for repo in (project, ota)}
    for role in ('normal', 'rescue', 'public/system', 'public/rescue'):
        folder = work / role / 'usr/share/doc/armhpc'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'BUILD-SOURCES.json').write_text(json.dumps({**provenance,
            'version': version, 'display_version': display_version, 'revision': revision, 'release': cfg['release'], 'source_versions': source_versions,
            'source_commits': refs, 'compatibility': compatibility,
            'fixed_boot': json.loads((work / 'boot-manifest.json').read_text())}, indent=2) + '\n')
        for repo in (project, ota):
            for name in ('LICENSE', 'COPYRIGHT'):
                shutil.copy2(repo / name, folder / (repo.name + '-' + name))
    run('compare-squashfs.py', '--root', work / 'public/rescue', '--ota-source', ota,
        '--output', work / 'compression', '--epoch', cfg['epoch'], '--only', 'xz-1m')
    firmware = output / 'firmware'
    assemble = 'assemble-oesp-squashfs.py' if cfg['board'] == 'wxy-oesp-v22' else 'assemble-oect-squashfs.py'
    run(assemble, '--project', project, '--ota-source', ota, '--work', work, '--output', firmware,
        '--base-image', cfg['base_image'], '--base-sha256', cfg['base_sha256'],
        '--base-provenance', cfg['base_provenance'], '--private-directory', cfg['private_directory'],
        '--signing-key', cfg['signing_key'], '--key-id', cfg['key_id'], '--boot-manifest', work / 'boot-manifest.json',
        '--release', cfg['release'], '--sequence', cfg['sequence'], '--epoch', cfg['epoch'],
        '--stem', cfg['stem'], '--asset-base-url', cfg.get('asset_base_url', 'https://payload.armhpc.invalid/' + cfg['release']))
    package = output / 'package'
    manifest = firmware / 'ota' / (cfg['release'] + '.manifest.json')
    trust = work / 'normal/etc/armhpc/ota-trust.d'
    run('prepare-firmware-package.py', '--project', project, '--ota-source', ota,
        '--manifest', manifest, '--artifacts', firmware / 'ota', '--trust', trust,
        '--roots', work, '--source-manifest', cfg['source_manifest'], '--provenance', cfg['provenance'], '--output', package)
    if cfg.get('kernel_work'):
        sys.path.insert(0,str(ota/'src'))
        from armhpc_ota.boot_publish import build as boot_bundle
        signed=json.loads(manifest.read_text())['payload']
        def artifact(role):
            item=next(a for a in signed['artifacts'] if a['role']==role)
            return firmware/'ota'/item['url'].rsplit('/',1)[-1]
        boot_bundle(manifest,artifact('rescue-rootfs'),artifact('rescue-kernel'),artifact('rescue-dtb'),
                    artifact('rescue-initramfs'),work/'boot/recovery-initramfs',
                    package/'firmware/boot-update.tar.xz',cfg['signing_key'],cfg['key_id'],trust,
                    cfg['sequence'],work/'boot/recovery-kernel',work/'boot/recovery-dtb',
                    system_rootfs=artifact('system-rootfs'),purpose='firmware-update')
        (package/'README.md').write_text('# ARMHPC firmware OTA\n\n[简体中文](README.zh-CN.md)\n\n'
            'Import this archive in the matching controller and select all. Install the supplied maintenance package first. '
            'The controller exports a verified private BOOT recovery backup before switching kernels, then updates paired '
            'Rescue and SATA system resources under one persistent task. Retain that backup until acceptance. '
            'Read the upgrade and rollback guide before starting.\n')
        (package/'README.zh-CN.md').write_text('# ARMHPC 固件 OTA\n\n'
            '在配套上位机导入本包并选择 all，先安装随附维护包。上位机先导出已核验的本机私有 BOOT 恢复备份，'
            '再以同一持久任务更新配套内核、Rescue 和 SATA 普通系统。验收前保留备份，并先阅读升级与回滚说明。\n')
        (package/'SHA256SUMS').write_text(''.join(sha(p)+'  '+p.relative_to(package).as_posix()+'\n'
            for p in sorted(package.rglob('*')) if p.is_file() and p.name!='SHA256SUMS'))
    image = firmware / (cfg['stem'] + '.img')
    destination = firmware
    extra = []
    if cfg['board'] == 'wxy-oesp-v22':
        sys.path.insert(0, str(ota / 'src'))
        from armhpc_ota.metadata import sign
        payload = json.loads(manifest.read_text())['payload']
        image_manifest = output / 'image.manifest.json'
        image_manifest.write_bytes(sign({'protocol': 1, 'kind': 'image', 'release': cfg['release'],
            'board': payload['board'], 'architecture': 'arm64', 'layout': payload['layout'],
            'anchor_abi': payload['anchor_abi'], 'image': {'bytes': image.stat().st_size, 'sha256': sha(image)},
            'hardware_verified': False}, cfg['signing_key'], cfg['key_id']))
        destination = output / 'native'
        run('package-oesp-burn.py', '--image', image, '--manifest', image_manifest, '--trust', trust,
            '--ota-source', ota, '--reference-image', cfg['reference_image'], '--packer', cfg['packer'],
            '--output', destination, '--name', cfg['stem'] + '.img')
        image = destination / image.name
        extra = ['--reuse-compressed-image']
    run('pack-firmware.py', '--project', project, '--ota-source', ota, '--package-root', package,
        '--image', image, '--output', destination, '--trust', trust, '--stem', cfg['stem'], *extra)
    assets = [destination / (cfg['stem'] + '.img.xz'),
              destination / (cfg['stem'].replace('armhpc_', 'armhpc-ota_', 1) + '.tar.xz')]
    result = {'version': version, 'display_version': display_version, 'revision': revision, 'release': cfg['release'], 'sequence': cfg['sequence'],
              'source_versions': source_versions, 'source_commits': refs,
              'compatibility': compatibility, 'hardware_verified': False,
              'image_redistribution': provenance.get('image_redistribution', {'reviewed': False}),
              'packages': [{'file': p.name, 'path': str(p), 'size': p.stat().st_size, 'sha256': sha(p)} for p in assets]}
    (output / 'FIRMWARE-RELEASE.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    print(json.dumps(build(**vars(parser.parse_args())), indent=2))
