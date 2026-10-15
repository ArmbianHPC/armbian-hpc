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


def release(repo, tag, release_id=None):
    suffix = str(int(release_id)) if release_id is not None else 'tags/' + tag
    result = json.loads(subprocess.check_output(
        ['gh', 'api', f'repos/{repo}/releases/{suffix}'], text=True, encoding='utf-8'))
    if result['tag_name'] != tag:
        raise ValueError('Release ID does not belong to the requested tag')
    return result


def publish_packages(repo, tag, paths, notes, *, apply=False, release_id=None, publish=False):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ValueError('Release tag must be vMAJOR.MINOR.PATCH')
    notes = Path(notes)
    if not notes.is_file() or not notes.with_name(notes.stem + '.zh-CN.md').is_file():
        raise ValueError('English and Simplified Chinese release notes are required')
    if not paths or len({p.name for p in paths}) != len(paths):
        raise ValueError('Provide one complete package set without duplicate names')
    if not all(p.is_file() and not p.is_symlink() and p.stat().st_size for p in paths):
        raise ValueError('All packages must be nonempty regular files before publishing')
    records = [{'file': p.name, 'size': p.stat().st_size, 'sha256': sha256(p)} for p in paths]
    current = release(repo, tag, release_id)
    release_id = current['id']
    expected = {p.name for p in paths}
    known = {a['name']: a for a in current['assets']}
    obsolete = [a for a in current['assets'] if a['name'] not in expected]
    for row in records:
        old = known.get(row['file'])
        if old and (old['size'] != row['size'] or old.get('digest') != 'sha256:' + row['sha256']):
            raise ValueError('Existing versioned package differs; use a new version')
    result = {'title': tag, 'release_id': release_id, 'packages': records,
              'remove_assets': [a['name'] for a in obsolete], 'publish': publish}
    if not apply:
        return result
    upload_url = current['upload_url'].split('{', 1)[0]
    from urllib.parse import quote
    for path in paths:
        if path.name not in known:
            subprocess.run(['gh', 'api', '--method', 'POST',
                upload_url + '?name=' + quote(path.name, safe=''),
                '-H', 'Content-Type: application/octet-stream', '--input', str(path)],
                check=True, stdout=subprocess.DEVNULL)
    present = {a['name']: a for a in release(repo, tag, release_id)['assets']}
    for row in records:
        asset = present.get(row['file'])
        if not asset or asset['size'] != row['size'] or asset.get('digest') != 'sha256:' + row['sha256']:
            raise ValueError('Uploaded package verification failed; old assets retained')
    body = {'name': tag, 'body': notes.read_text(encoding='utf-8')}
    subprocess.run(['gh', 'api', '--method', 'PATCH', f'repos/{repo}/releases/{release_id}',
                    '--input', '-'], input=json.dumps(body), text=True,
                   stdout=subprocess.DEVNULL, check=True)
    for asset in obsolete:
        subprocess.run(['gh', 'api', '--method', 'DELETE',
                        f'repos/{repo}/releases/assets/{asset["id"]}'], check=True)
    final = release(repo, tag, release_id)
    if final['name'] != tag or {a['name'] for a in final['assets']} != expected:
        raise ValueError('Final Release does not match the complete package set')
    if publish:
        subprocess.run(['gh', 'api', '--method', 'PATCH', f'repos/{repo}/releases/{release_id}',
            '--input', '-'], input=json.dumps({'draft': False, 'prerelease': False}), text=True,
            stdout=subprocess.DEVNULL, check=True)
        final = release(repo, tag, release_id)
        if final['draft'] or final['prerelease']:
            raise ValueError('Release did not become a formal public release')
    return {**result, 'url': final['html_url'], 'assets': len(final['assets']), 'draft': final['draft']}


def manifest_files(path, tag):
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    if document.get('tag') != tag:
        raise ValueError('Package manifest tag differs')
    packages = document.get('packages', [])
    groups = {}
    for row in packages:
        match = re.fullmatch(r'(armhpc|armhpc-ota)_' + re.escape(tag[1:]) +
            r'_([a-zA-Z0-9.-]+)_([a-zA-Z0-9.-]+)_(\d+\.\d+\.\d+)_([a-zA-Z0-9.-]+)_(\d{8})\.(img|tar)\.xz', row['file'])
        if not match:
            raise ValueError('Invalid firmware manifest filename')
        prefix, model, distribution, kernel, filesystem, date, extension = match.groups()
        expected = names(tag[1:], model, distribution, kernel, filesystem, date)
        if row['file'] not in expected or (prefix == 'armhpc') != (extension == 'img'):
            raise ValueError('Firmware filename role differs')
        if not re.fullmatch(r'[0-9a-f]{64}', row.get('sha256', '')) or not isinstance(row.get('size'), int) or row['size'] <= 0:
            raise ValueError('Every package needs a SHA256 and byte size')
        key = (model, distribution, kernel, filesystem, date)
        groups.setdefault(key, []).append(row['file'])
        if extension == 'img':
            permission = row.get('redistribution', {})
            if permission.get('reviewed') is not True or not permission.get('evidence'):
                raise ValueError('Image redistribution needs reviewed component evidence: ' + model)
    if not groups or len({r['file'] for r in packages}) != len(packages):
        raise ValueError('Empty or duplicate firmware package manifest')
    for key, actual in groups.items():
        if set(actual) != set(names(tag[1:], *key)):
            raise ValueError('Each model needs both its complete image and OTA archive')
    return packages


def update(repo, tag, directory, model=None, distribution='bookworm', kernel=None,
           filesystem='squashfs', build_date=None, notes=None, apply=False,
           manifest=None, release_id=None, publish=False):
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ValueError('Release tag must be vMAJOR.MINOR.PATCH')
    directory = Path(directory).resolve(strict=True)
    if not manifest:
        raise ValueError('Supply --manifest with every model and both packages; per-model updates are unsafe')
    rows = manifest_files(manifest, tag)
    paths = [directory / row['file'] for row in rows]
    for row, path in zip(rows, paths):
        if not path.is_file() or path.is_symlink() or path.stat().st_size != row['size'] or sha256(path) != row['sha256']:
            raise ValueError('Local package differs from the release manifest: ' + path.name)
    notes = notes or Path(__file__).resolve().parents[1] / 'docs/releases' / (tag + '.md')
    return publish_packages(repo, tag, paths, notes, apply=apply, release_id=release_id, publish=publish)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='ArmbianHPC/armbian-hpc')
    parser.add_argument('--tag', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--notes', type=Path)
    parser.add_argument('--release-id', type=int)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--publish', action='store_true')
    print(json.dumps(update(**vars(parser.parse_args())), indent=2))
