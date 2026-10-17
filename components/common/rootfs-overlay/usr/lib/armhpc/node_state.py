# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Inspect and restore explicitly registered node resources from verified DATA."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from config_io import (approve, atomic_write, configuration_lock, emit, entry,
                       normal_system, read_json, require, require_idle, run,
                       safe_path, save_json, sync_directory)

CATALOG = 'node-config/armhpc/state.json'
RESOURCES = 'node-config/armhpc/resources'
UNIT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.@-]*\.(?:service|timer)')
SWAP_REQUIREMENTS={'CONFIG_SWAP','CONFIG_BLK_DEV_LOOP','CONFIG_MD','CONFIG_BLK_DEV_DM',
                   'CONFIG_DM_CRYPT','CONFIG_CRYPTO_AES','CONFIG_CRYPTO_XTS'}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def data_mount(root=Path('/'), expected=None):
    """Never create DATA-backed state before validating the actual mount."""
    root = Path(root)
    target = root / 'data'
    rows = json.loads(run('findmnt', '-J', '-M', target, '-o', 'TARGET,SOURCE,FSTYPE,UUID,OPTIONS'))['filesystems']
    require(len(rows) == 1, 'data_mount', 'DATA mount is missing or ambiguous')
    value = rows[0]
    require(value['target'] == str(target) and value['fstype'] == 'ext4' and value.get('uuid')
            and 'rw' in value['options'].split(','), 'data_mount', 'DATA must be a writable, UUID-identified ext4 mount')
    local=read_json(safe_path(root,'etc/armhpc/data-identity.json'),{})
    if local:
        require(value['uuid']==local.get('uuid'),'data_identity','DATA UUID differs from this system\'s retained identity')
    if expected:
        require(value['uuid'] == expected['uuid'], 'data_identity', 'DATA UUID differs from the registered device')
    disk = json.loads(run('lsblk', '-J', '-s', '-o', 'PATH,TYPE,SERIAL,WWN,TRAN', value['source']))
    def flatten(rows):
        for item in rows:
            yield item
            yield from flatten(item.get('children', []))
    parents = [item for item in flatten(disk['blockdevices']) if item.get('type') == 'disk']
    require(len(parents) == 1 and parents[0].get('tran') == 'sata', 'data_disk', 'DATA must belong to one SATA disk')
    identity = {key: parents[0].get(key) for key in ('serial', 'wwn')}
    require(any(identity.values()), 'data_identity', 'DATA disk needs a serial number or WWN')
    if expected:
        require(all(not expected.get(k) or expected[k] == v for k, v in identity.items()),
                'data_identity', 'DATA disk identity changed')
    if local:
        require(all(not local.get(k) or local[k]==v for k,v in identity.items()),
                'data_identity','DATA disk identity differs from the retained system record')
    return target, {'uuid': value['uuid'], **identity}


def bind_identity(root,identity):
    path=safe_path(root,'etc/armhpc/data-identity.json')
    previous=read_json(path,{})
    require(not previous or previous==identity,'data_identity','Changing DATA needs explicit disk installation/recovery')
    if not previous:save_json(path,identity)


def kernel_status(root=Path('/'), release=None):
    root = Path(root)
    release = release or os.uname().release
    policy = read_json(root / 'etc/image-framework/kernel-requirements.json', {})
    baseline=read_json(root/'usr/lib/armhpc/kernel-baseline.json',{})
    if baseline:
        require(baseline.get('schema_version')==1 and isinstance(baseline.get('requirements'),dict),
                'kernel_baseline','Unsupported shared kernel baseline')
        policy={**policy,**baseline['requirements']}
    compressed = root / 'proc/config.gz'
    candidates = (root / 'boot' / ('config-' + release), root / 'usr/lib/modules' / release / 'config')
    text = gzip.decompress(compressed.read_bytes()).decode() if compressed.is_file() else next(
        (p.read_text() for p in candidates if p.is_file()), '')
    values = dict(line.split('=', 1) for line in text.splitlines() if line.startswith('CONFIG_') and '=' in line)
    missing = {key: values.get(key, 'n') for key, allowed in policy.items() if values.get(key) not in allowed}
    modules = root / 'lib/modules' / release
    groups={'cpu_quota':{'CONFIG_CFS_BANDWIDTH','CONFIG_CGROUP_SCHED'},
            'seccomp':{'CONFIG_SECCOMP','CONFIG_SECCOMP_FILTER'},
            'network_policy':{'CONFIG_IP_SET','CONFIG_NETFILTER_XT_SET','CONFIG_NF_CONNTRACK'},
            'policy_routing':{'CONFIG_IP_MULTIPLE_TABLES','CONFIG_IPV6_MULTIPLE_TABLES'},
            'encrypted_swap':SWAP_REQUIREMENTS}
    features={name:{'ready':bool(text and policy and not (set(missing)&keys)),
                    'missing':sorted(set(missing)&keys)} for name,keys in groups.items()}
    return {'release': release, 'config_available': bool(text),
            'config_sha256': hashlib.sha256(text.encode()).hexdigest() if text else None,
            'modules_available': modules.is_dir(), 'missing': missing,'features':features,
            'containers_ready':bool(text and policy and not (set(missing)-SWAP_REQUIREMENTS) and modules.is_dir()),
            'ready': bool(text and policy and not missing and modules.is_dir())}


def resource_path(root, relative, allow_link=False):
    require(isinstance(relative, str) and not relative.startswith('/') and '\\' not in relative,
            'resource_path', 'Use a root-relative managed resource path')
    parts = Path(relative).parts
    allowed = (relative.startswith(('usr/local/bin/', 'usr/local/sbin/', 'usr/lib/armhpc/',
                                   'etc/systemd/system/', 'etc/tmpfiles.d/', 'etc/modules-load.d/',
                                   'etc/sysctl.d/', 'etc/rancher/', 'etc/armhpc/')))
    allowed=allowed or relative.startswith(('data/node-config/k3s/','data/k3s/agent/etc/kubelet.conf.d/'))
    require(allowed and len(parts) > 2 and not any(p in ('', '.', '..') for p in parts),
            'resource_path', 'Resource lies outside managed program/configuration directories')
    if allow_link:
        return safe_path(root, str(Path(relative).parent)) / Path(relative).name
    return safe_path(root, relative)


def load(root=Path('/')):
    data, identity = data_mount(root)
    path = safe_path(data, CATALOG)
    for parent in (path, *path.parents):
        if parent.is_relative_to(data) and parent.exists():
            require(parent.stat().st_uid == 0 and not parent.stat().st_mode & 0o022,
                    'state_permissions', 'DATA recovery resources must be root-owned and not publicly writable')
    value = read_json(path, {'schema_version': 1, 'data': identity, 'services': {}})
    require(value.get('schema_version') == 1 and isinstance(value.get('services'), dict),
            'state_schema', 'Unsupported node state schema')
    require(isinstance(value.get('data'),dict) and isinstance(value['data'].get('uuid'),str),
            'state_schema','Recovery catalog has no valid DATA identity')
    require(all(isinstance(service,dict) and isinstance(service.get('enabled'),bool)
                and isinstance(service.get('files'),list) and len(service['files'])<=128
                for service in value['services'].values()),'state_schema','Malformed registered service record')
    data_mount(root, value['data'])
    return data, path, value


def register(root, manifest, apply=True):
    data, path, state = load(root)
    require(UNIT.fullmatch(manifest.get('unit', '')) and isinstance(manifest.get('enabled'), bool),
            'service_manifest', 'Specify a service/timer unit and explicit enabled state')
    require(manifest['unit'] not in ('armhpc-state.service','armhpc-runtime.service','armhpc-swap.service',
                                    'armhpc-auth-recovery.service','armhpc-health.service','armhpc-health.timer'),
            'service_manifest', 'Foundation services cannot depend on their own restore barrier')
    require(isinstance(manifest.get('files'), list) and 0 < len(manifest['files']) <= 128,
            'service_manifest', 'Supply 1–128 explicitly owned program/unit/configuration files')
    require('etc/systemd/system/' + manifest['unit'] in manifest['files'],
            'service_manifest', 'The service unit must be included')
    dependency='etc/systemd/system/'+manifest['unit']+'.d/10-armhpc-state.conf'
    dependency_path=resource_path(root,dependency)
    dependency_text='# Managed by armhpc state\n[Unit]\nRequires=armhpc-runtime.service armhpc-swap.service\nAfter=armhpc-runtime.service armhpc-swap.service\nRequiresMountsFor=/data\n'
    require(not dependency_path.exists() or dependency_path.read_text()==dependency_text,
            'service_conflict','An unrelated dependency drop-in already exists')
    files, contents = [], {}
    declared = set(manifest['files'])
    for relative in dict.fromkeys([*manifest['files'],dependency]):
        source = resource_path(root, relative, allow_link=True)
        if relative == dependency:
            content, mode = dependency_text.encode(), 0o644
        elif source.is_symlink():
            target = os.readlink(source)
            require(not Path(target).is_absolute() and '\\' not in target,
                    'resource_link', 'Registered program links must have a relative target')
            linked = (source.parent / target).resolve()
            require(linked.is_relative_to(Path(root).resolve()) and
                    linked.relative_to(Path(root).resolve()).as_posix() in declared and linked.is_file(),
                    'resource_link', 'Include the target of every registered symbolic link')
            files.append({'path': relative, 'kind': 'symlink', 'target': target})
            continue
        else:
            require(source.is_file() and source.stat().st_size <= 256 * 1024**2,
                'service_resource', 'Missing or oversized resource: ' + relative)
            require(source.stat().st_uid == 0 and not source.stat().st_mode & 0o7022,
                'service_permissions', 'Registered resources must be root-owned and not writable by other users')
            require(not os.listxattr(source), 'resource_attributes',
                    'Import resources with capabilities or extended attributes through a package-specific adapter')
            mode = source.stat().st_mode & 0o777
        checksum = hashlib.sha256(content).hexdigest() if relative==dependency else digest(source)
        contents[checksum] = content if relative==dependency else source
        files.append({'path': relative, 'sha256': checksum, 'mode': mode})
    require(shutil.disk_usage(data).free >= sum(len(value) if isinstance(value,bytes) else value.stat().st_size for value in contents.values()) + 64 * 1024**2,
            'state_space', 'Insufficient DATA space for a recoverable resource copy')
    if not apply:return {'unit':manifest['unit'],'enabled':manifest['enabled'],'files':files,'applied':False}
    for checksum, content in contents.items():
        cache = safe_path(data, RESOURCES + '/' + checksum)
        if not cache.exists():
            atomic_write(cache, content if isinstance(content,bytes) else content.read_bytes(), 0o600)
        require(digest(cache) == checksum, 'resource_hash', 'Recovery resource checksum differs')
    atomic_write(dependency_path,dependency_text)
    old = path.read_bytes() if path.exists() else None
    if old:
        atomic_write(path.with_name('state.previous.json'), old, 0o600)
    state['services'][manifest['unit']] = {
        'enabled': manifest['enabled'], 'files': files,
        'version': manifest.get('version'), 'source': manifest.get('source', 'administrator-registered')}
    save_json(path, state)
    bind_identity(root,state['data'])
    return state


def migrate_legacy(root=Path('/')):
    if Path(root)!=Path('/'):return
    policy_path=Path('/etc/image-framework/k3s.json')
    if not policy_path.is_file():return
    policy=read_json(policy_path)
    management=read_json(Path(policy['backups_dir'])/'k3s-management.json',{})
    require(management.get('stage')!='incomplete','state_management',
            'Continue the original K3s management operation before restoring dependent services')
    previous=read_json(Path(policy['state_file']),{})
    if previous.get('status')!='installed' or not previous.get('plan'):return
    _,_,catalog=load(root)
    from kube_runtime import installed,capture,check_configuration
    actual=installed(policy)
    if actual['service'] in catalog['services']:return
    require(actual['ready'],'legacy_repair_required',
            'Existing K3s installation is incomplete and has no verified recovery catalog; restore the exact installed version before importing')
    check_configuration(Path(policy['configuration_dir'],'config.yaml').read_text())
    capture(policy,previous['plan'])


def inventory(root, data, state):
    changes, errors = [], []
    for unit, service in state['services'].items():
        require(UNIT.fullmatch(unit), 'state_schema', 'Invalid registered unit')
        for item in service['files']:
            require(isinstance(item,dict) and isinstance(item.get('path'),str),'state_schema','Invalid resource path record')
            if item.get('kind') == 'symlink':
                destination = resource_path(root, item['path'], allow_link=True)
                target = item['target']
                require(isinstance(target,str) and not Path(target).is_absolute() and '\\' not in target,
                        'resource_link', 'Invalid cached symbolic-link target')
                linked = (destination.parent / target).resolve()
                require(linked.is_relative_to(Path(root).resolve()) and
                        linked.relative_to(Path(root).resolve()).as_posix() in {r['path'] for r in service['files']},
                        'resource_link', 'Symbolic-link target is outside registered resources')
                if not destination.exists() and not destination.is_symlink():
                    changes.append({'unit': unit, **item})
                elif not destination.is_symlink() or os.readlink(destination) != target:
                    errors.append({'unit':unit,'path':item['path'],'reason':'resource_conflict'})
                continue
            destination = resource_path(root, item['path'])
            checksum = item.get('sha256')
            require(isinstance(checksum,str) and isinstance(item.get('mode'),int) and 0<=item['mode']<=0o777,
                    'state_schema','Invalid checksum or permissions in recovery catalog')
            require(re.fullmatch('[0-9a-f]{64}', checksum) is not None,
                    'resource_hash', 'Malformed resource checksum')
            cache = safe_path(data, RESOURCES + '/' + checksum)
            if not cache.is_file() or cache.stat().st_uid!=0 or cache.stat().st_mode&0o077 or digest(cache) != checksum:
                errors.append({'unit': unit, 'path': item['path'], 'reason': 'verified_resource_missing'})
            elif not destination.exists():
                changes.append({'unit': unit, **item})
            elif not destination.is_file() or destination.stat().st_uid!=0 or destination.stat().st_mode&0o7777!=item['mode'] or os.listxattr(destination) or digest(destination) != checksum:
                errors.append({'unit': unit, 'path': item['path'], 'reason': 'resource_conflict'})
    return changes, errors


def status(root=Path('/')):
    try:
        data, _, state = load(root)
        changes, errors = inventory(root, data, state)
        services = {}
        if Path(root) == Path('/') and state['services']:
            result = subprocess.run(['systemctl', 'show', *state['services'], '-p', 'Id', '-p', 'LoadState', '-p', 'ActiveState', '-p', 'Result',
                                     '-p','Type','-p','ExecMainStatus','-p','ExecMainStartTimestampMonotonic'],
                                    text=True, capture_output=True, timeout=3)
            for block in result.stdout.strip().split('\n\n'):
                fields=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
                if fields.get('Id') in state['services']:services[fields['Id']]=fields
        return {'schema_version': 1, 'data': state['data'], 'desired': {
            unit: {'enabled': item['enabled'], 'version': item.get('version')} for unit, item in state['services'].items()},
            'installed': {'ready': not changes and not errors, 'missing': changes, 'errors': errors}, 'running': services}
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        return {'schema_version': 1, 'installed': {'ready': False, 'errors': [{'reason': getattr(error, 'code', 'state_io'),
                                                                             'message': str(error)}]}}


def reconcile(root=Path('/'), apply=False):
    data, path, state = load(root)
    changes, errors = inventory(root, data, state)
    require(not errors, 'state_repair_required', json.dumps(errors, ensure_ascii=False))
    if apply:
        bind_identity(root,state['data'])
        if not path.exists():save_json(path,state)
        for item in changes:
            if item.get('kind') == 'symlink':
                destination = resource_path(root,item['path'])
                destination.parent.mkdir(parents=True,exist_ok=True)
                temporary=destination.with_name('.'+destination.name+'.armhpc-link')
                require(not temporary.exists() and not temporary.is_symlink(),'resource_conflict','Temporary recovery link already exists')
                temporary.symlink_to(item['target'])
                os.replace(temporary,destination)
                sync_directory(destination.parent)
                continue
            atomic_write(resource_path(root, item['path']),
                         safe_path(data, RESOURCES + '/' + item['sha256']).read_bytes(), item['mode'])
        for unit, item in state['services'].items():
            run('systemctl', '--root', root, 'enable' if item['enabled'] else 'disable', unit)
        if Path(root) == Path('/'):
            run('systemctl', 'daemon-reload')
        save_json(path.with_name('last-reconcile.json'), {'schema_version': 1, 'restored': changes})
    return {'changes': changes, 'applied': apply, 'services': list(state['services'])}


def prepare_runtime():
    data, _, state = load()
    _, errors = inventory(Path('/'), data, state)
    require(not errors, 'state_runtime', 'Registered runtime resources are not verified')
    paths = {'/etc/tmpfiles.d/armhpc-runtime.conf'}
    for item in state['services'].values():
        paths.update('/'+resource['path'] for resource in item['files'] if resource['path'].startswith('etc/tmpfiles.d/'))
    run('systemd-tmpfiles','--create',*sorted(paths))
    resources={resource['path'] for item in state['services'].values() for resource in item['files']}
    for relative in sorted(resources):
        if relative.startswith('etc/modules-load.d/'):
            for line in resource_path(Path('/'),relative).read_text().splitlines():
                module=line.split('#',1)[0].strip()
                if module:
                    require(re.fullmatch(r'[A-Za-z0-9_-]+',module),'runtime_module','Invalid module name in '+relative)
                    run('modprobe',module,timeout=5)
    for relative in sorted(resources):
        if relative.startswith('etc/sysctl.d/'):
            run('sysctl','-p',resource_path(Path('/'),relative),timeout=5)
    enabled=[unit for unit,item in state['services'].items() if item['enabled']]
    if enabled:run('systemctl','start','--no-block',*enabled,timeout=5)


def main():
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG', 'armhpc state'))
    parser.add_argument('action', nargs='?', default='status', choices=('status', 'reconcile', 'register', '_boot', '_runtime'))
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if args.action == 'status':
        result = status()
    elif args.action == '_runtime':
        normal_system(Path('/'))
        prepare_runtime()
        result = {'runtime_ready':True}
    else:
        normal_system(Path('/'))
        with configuration_lock(Path('/')):
            if args.action != '_boot':
                require_idle(Path('/'))
                if args.apply:approve(args.yes, 'Register or restore the explicitly selected node resources?')
            if args.action == 'register':
                require(args.manifest is not None, 'service_manifest', 'Supply --manifest')
                result = register(Path('/'), read_json(args.manifest),args.apply)
            else:
                if args.action=='_boot':migrate_legacy()
                result = reconcile(apply=args.apply or args.action == '_boot')
    emit(result, args.json)


if __name__ == '__main__':
    raise SystemExit(entry(main))
