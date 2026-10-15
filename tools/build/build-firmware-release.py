#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Build a paired firmware release from an explicit, private build-input JSON."""
import argparse
import hashlib
import json
import os
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
    if (ota / 'VERSION').read_text().strip() != version:
        raise ValueError('Use matching framework and OTA software versions')
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
    run('refresh-fixed-boot-roots.py', '--project', project, '--ota-source', ota,
        '--input-roots', cfg['input_roots'], '--boot-manifest', cfg['boot_manifest'],
        '--work', work, '--watchdog-binary', cfg['watchdog_binary'], '--board', cfg['board'],
        '--release', cfg['release'], '--epoch', cfg['epoch'])
    provenance = json.loads(Path(cfg['provenance']).read_text())
    refs = {repo.name: subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
            for repo in (project, ota)}
    for role in ('normal', 'rescue', 'public/system', 'public/rescue'):
        folder = work / role / 'usr/share/doc/armhpc'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'BUILD-SOURCES.json').write_text(json.dumps({**provenance,
            'version': version, 'release': cfg['release'], 'source_commits': refs,
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
    result = {'version': version, 'release': cfg['release'], 'sequence': cfg['sequence'],
              'source_commits': refs, 'hardware_verified': False,
              'image_redistribution': provenance.get('image_redistribution', {'reviewed': False}),
              'packages': [{'file': p.name, 'path': str(p), 'size': p.stat().st_size, 'sha256': sha(p)} for p in assets]}
    (output / 'FIRMWARE-RELEASE.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    print(json.dumps(build(**vars(parser.parse_args())), indent=2))
