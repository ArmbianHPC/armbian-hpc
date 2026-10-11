# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only
"""Publish an image and OTA archive with version-only English release notes."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess


def names(version, model, distribution, kernel, filesystem, build_date):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Version must be MAJOR.MINOR.PATCH')
    if any(not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]*', value)
           for value in (model, distribution, kernel, filesystem)):
        raise ValueError('Invalid firmware filename component')
    if not re.fullmatch(r'\d{8}', build_date):
        raise ValueError('Build date must be YYYYMMDD')
    datetime.strptime(build_date, '%Y%m%d')
    suffix = '_'.join((version, model, distribution, kernel, filesystem, build_date))
    return ['armhpc_' + suffix + '.img.xz', 'armhpc-ota_' + suffix + '.tar.xz']


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def release(repo, tag):
    return json.loads(subprocess.check_output(
        ['gh', 'api', f'repos/{repo}/releases/tags/{tag}'], text=True, encoding='utf-8'))


def update(repo, tag, directory, model, distribution, kernel, filesystem, build_date,
           notes=None, apply=False):
    if not tag.startswith('v'):
        raise ValueError('Release tag must start with v')
    expected = names(tag[1:], model, distribution, kernel, filesystem, build_date)
    directory = Path(directory).resolve(strict=True)
    notes = Path(notes) if notes else Path(__file__).resolve().parents[1] / 'docs/releases' / (tag + '.md')
    if not notes.is_file():
        raise ValueError('English release notes are missing')
    paths = [directory / name for name in expected]
    if not all(path.is_file() and not path.is_symlink() for path in paths):
        raise ValueError('Both firmware packages must exist before publishing')
    records = [{'file': path.name, 'size': path.stat().st_size, 'sha256': sha256(path)} for path in paths]
    current = release(repo, tag)
    known = {asset['name']: asset for asset in current['assets']}
    obsolete = [asset for asset in current['assets'] if asset['name'] not in expected]
    for record in records:
        old = known.get(record['file'])
        if old and (old['size'] != record['size'] or old.get('digest') != 'sha256:' + record['sha256']):
            raise ValueError('Existing versioned package differs; publish a new version')
    result = {'title': tag, 'packages': records, 'remove_assets': [a['name'] for a in obsolete]}
    if not apply:
        return result
    for path in paths:
        if path.name not in known:
            subprocess.run(['gh', 'release', 'upload', tag, str(path), '--repo', repo], check=True)
    present = {a['name']: a for a in release(repo, tag)['assets']}
    for record in records:
        asset = present[record['file']]
        if asset['size'] != record['size'] or asset.get('digest') != 'sha256:' + record['sha256']:
            raise ValueError('Uploaded package verification failed; previous assets retained')
    subprocess.run(['gh', 'release', 'edit', tag, '--repo', repo, '--title', tag,
                    '--notes-file', str(notes)], check=True)
    for asset in obsolete:
        subprocess.run(['gh', 'api', '--method', 'DELETE',
                        f'repos/{repo}/releases/assets/{asset["id"]}'], check=True)
    final = release(repo, tag)
    if final['name'] != tag or set(a['name'] for a in final['assets']) != set(expected):
        raise ValueError('Final Release does not follow the two-package policy')
    return {**result, 'url': final['html_url'], 'assets': len(final['assets'])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='ArmbianHPC/armbian-hpc')
    parser.add_argument('--tag', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--model', default='wxy-oect')
    parser.add_argument('--distribution', default='bookworm')
    parser.add_argument('--kernel', required=True)
    parser.add_argument('--filesystem', default='squashfs')
    parser.add_argument('--build-date', required=True)
    parser.add_argument('--notes', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = vars(parser.parse_args())
    print(json.dumps(update(**args), indent=2))
