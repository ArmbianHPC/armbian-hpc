# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""One container feature baseline for every board and build entry point."""
import hashlib
import json
from pathlib import Path


def requirements(project):
    return json.loads((Path(project) / 'profiles/kernel/kubernetes.json').read_text())['requirements']


def parse_config(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if line.startswith('CONFIG_') and '=' in line:
            key, value = line.split('=', 1)
            result[key] = value
    return result


def validate(project, config):
    actual = parse_config(config)
    missing = {key: {'actual': actual.get(key, 'n'), 'allowed': allowed}
               for key, allowed in requirements(project).items() if actual.get(key) not in allowed}
    if missing:
        raise ValueError('Final kernel configuration lacks required capabilities: ' + json.dumps(missing, sort_keys=True))
    return {'config_sha256': hashlib.sha256(Path(config).read_bytes()).hexdigest(),
            'baseline_sha256': hashlib.sha256(json.dumps(requirements(project), sort_keys=True).encode()).hexdigest()}


def merge(project, source, obj, run):
    config = Path(obj) / '.config'
    command = [str(Path(source) / 'scripts/config'), '--file', str(config)]
    for key, allowed in requirements(project).items():
        command.extend(('--enable' if 'y' in allowed else '--module', key.removeprefix('CONFIG_')))
    run(*command)


def module_manifest(directory,release):
    directory=Path(directory)
    root=directory/'lib/modules'/release
    if not (root/'modules.dep').is_file() or not (root/'modules.builtin').is_file():
        raise ValueError('Matching module dependency/builtin indexes are missing')
    files={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
           for p in root.rglob('*') if p.is_file() and not p.is_symlink()}
    if not files:raise ValueError('No installed matching kernel modules')
    raw=json.dumps(files,sort_keys=True,separators=(',',':')).encode()
    return {'kernel_release':release,'sha256':hashlib.sha256(raw).hexdigest(),'files':files}
