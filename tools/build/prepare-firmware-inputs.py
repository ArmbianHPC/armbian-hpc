#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Fetch hash-pinned redistributable board firmware without fetching private boot inputs."""
import argparse
import json
from pathlib import Path
import shutil
import sys


def prepare(project, boards=None, verify_only=False, from_directory=None):
    project = Path(project).resolve(strict=True)
    sys.path.insert(0, str(project))
    from framework.sources import prepare_sources
    items = []
    for path in sorted((project / 'adapters/boards').glob('*.json')):
        board = json.loads(path.read_text(encoding='utf-8'))
        if boards and board['id'] not in boards:
            continue
        for row in board.get('firmware', []):
            if row.get('redistributable') is not True or row.get('reviewed') is not True:
                raise ValueError('Board firmware lacks redistribution review: ' + row['id'])
            license_file = project / row['license_file']
            if not license_file.is_file():
                raise ValueError('Missing firmware license: ' + row['id'])
            if from_directory and not verify_only:
                from framework.config import sha256
                source = Path(from_directory) / Path(row['path']).name
                if source.is_symlink() or not source.is_file() or sha256(source) != row['sha256']:
                    raise ValueError('Explicit offline firmware input differs: ' + row['id'])
                target = project / row['path']
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name('.' + target.name + '.prepared')
                    with temporary.open('xb') as destination, source.open('rb') as stream:
                        shutil.copyfileobj(stream, destination)
                    if sha256(temporary) != row['sha256']:
                        temporary.unlink()
                        raise ValueError('Copied firmware hash differs')
                    temporary.replace(target)
            items.append({**row, 'owner': board['id']})
    resolved = {'target': {'id': 'software-firmware'}, 'architecture': 'arm64',
                'inputs': [], 'firmware': items}
    return prepare_sources(project, resolved, verify_only=verify_only)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--board', dest='boards', action='append')
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--from-directory', type=Path, help='Explicit offline firmware inputs, checked against board SHA256')
    print(json.dumps(prepare(**vars(parser.parse_args())), indent=2))
