# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""K3s capability checks and recoverable installation resources."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from config_io import require, run, safe_path, save_json, read_json


def check_configuration(text):
    import yaml
    class UniqueLoader(yaml.SafeLoader):
        pass
    def mapping(loader, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            require(key not in result, 'k3s_duplicate_key', 'Duplicate YAML key: ' + str(key))
            result[key] = loader.construct_object(value_node, deep=deep)
        return result
    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    value = yaml.load(text, Loader=UniqueLoader)
    require(isinstance(value, dict), 'k3s_configuration', 'K3s configuration must be a mapping')
    for label in value.get('node-label', []):
        require(not label.startswith('node-role.kubernetes.io/'), 'k3s_label', 'Reserved role labels must be applied by a cluster administrator')
    args = value.get('kubelet-arg', [])
    require(len(args) == len(set(item.split('=', 1)[0] for item in args)),
            'k3s_argument', 'Duplicate kubelet options')
    require(not any(item.startswith('address=') for item in args),
            'kubelet_listen', 'Keep the kubelet listener default; restrict access with the host firewall')
    return value


def preflight(policy, installing=False):
    from node_state import data_mount, kernel_status
    from network_ready import inspect, policy as management_policy
    _, identity = data_mount()
    kernel = kernel_status()
    require(kernel.get('containers_ready',kernel['ready']), 'kernel_capabilities',
            'Container/kernel capabilities are missing; see feature impacts: ' + json.dumps(kernel.get('features',kernel['missing'])))
    controllers = Path('/sys/fs/cgroup/cgroup.controllers').read_text().split()
    require({'cpu', 'memory', 'pids'} <= set(controllers), 'cgroup_controllers', 'cgroup v2 must provide cpu, memory and pids controllers')
    management = inspect(management_policy())
    require(management['ready'], 'management_network', json.dumps(management['errors']))
    require(Path(policy['data_mountpoint']).stat().st_dev != Path('/').stat().st_dev,
            'data_mount', 'K3s DATA must not be a directory on the root filesystem')
    import shutil
    require(not installing or shutil.disk_usage(policy['data_mountpoint']).free >= 2 * 1024**3,
            'data_space', 'K3s needs at least 2 GiB of free DATA space before installation')
    clock = subprocess.run(['/usr/bin/python3','/usr/lib/armhpc/clock_floor.py','--status'],
                           capture_output=True, text=True, timeout=10)
    require(clock.returncode == 0 and json.loads(clock.stdout)['data'].get('quality') in ('estimated','synchronised'),
            'clock_invalid', 'Restore a valid clock anchor before verified downloads or cluster admission')
    return {'data': identity, 'kernel': kernel, 'management_network': management}


def installed(policy):
    state_path = Path(policy['state_file'])
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    plan = state.get('plan', {})
    binary = Path('/usr/local/bin/k3s')
    service = 'k3s-agent.service' if plan.get('role') == 'agent' else 'k3s.service'
    errors = []
    version = None
    if not binary.is_file() or not os.access(binary, os.X_OK):
        errors.append('binary_missing')
    else:
        version = run(binary, '--version', timeout=15).splitlines()[0]
        if plan.get('version') and ('k3s version ' + plan['version'] + ' ') not in version + ' ':
            errors.append('binary_version_mismatch')
    if not Path('/etc/systemd/system', service).is_file():
        errors.append('service_missing')
    if not Path(policy['configuration_dir'], 'config.yaml').is_file():
        errors.append('configuration_missing')
    return {'ready': not errors, 'errors': errors, 'version': version, 'service': service,
            'desired': {'enabled': bool(plan), 'role': plan.get('role'), 'version': plan.get('version')}}


def capture(policy, plan):
    from node_state import register
    unit = 'k3s-agent.service' if plan['role'] == 'agent' else 'k3s.service'
    files = ['usr/local/bin/k3s', 'etc/systemd/system/' + unit]
    for prefix in ('etc/systemd/system/' + unit + '.d',):
        files.extend(str(p.relative_to('/')) for p in Path('/'+prefix).glob('*.conf') if p.is_file() and not p.is_symlink())
    for relative in ('etc/systemd/system/'+unit+'.env', 'usr/local/bin/k3s-killall.sh',
                     'usr/local/bin/k3s-uninstall.sh','usr/local/bin/k3s-agent-uninstall.sh',
                     'etc/sysctl.d/90-k3s.conf','etc/modules-load.d/90-k3s.conf'):
        if Path('/'+relative).is_file():
            files.append(relative)
    for name in ('kubectl','crictl','ctr'):
        path=Path('/usr/local/bin')/name
        if path.is_symlink() and path.resolve()==Path('/usr/local/bin/k3s'):
            files.append(str(path.relative_to('/')))
    for name in ('config.yaml','registries.yaml','registry-managed.json','join-token'):
        path=Path(policy['configuration_dir'])/name
        if path.is_file():files.append(str(path.relative_to('/')))
    files.extend(str(p.relative_to('/')) for p in (Path(policy['configuration_dir'])/'registry-files').glob('*.pem') if p.is_file())
    files.extend(str(p.relative_to('/')) for p in (Path(policy['data_dir'])/'agent/etc/kubelet.conf.d').glob('*armhpc*.conf') if p.is_file())
    require(installed(policy)['ready'], 'k3s_installation', 'Actual K3s installation is incomplete')
    return register(Path('/'), {'unit': unit, 'enabled': True, 'files': files,
                               'version': plan['version'], 'source': plan.get('download_source',{}).get('artifact_url','legacy-installed')})


def cluster_ready(policy, plan):
    """Only a Ready record for this node can trigger the post-join login reminder."""
    configs = [Path('/etc/rancher/k3s/k3s.yaml'), Path(policy['data_dir'])/'agent/kubelet.kubeconfig']
    config = next((p for p in configs if p.is_file()), None)
    if not config:
        return False
    result = subprocess.run(['/usr/local/bin/k3s','kubectl','--kubeconfig',str(config),
                             '--request-timeout=5s','get','node',plan['node_name'],'-o','json'],
                            text=True, capture_output=True, timeout=10)
    if result.returncode:
        return False
    value = json.loads(result.stdout)
    info=value.get('status',{}).get('nodeInfo',{})
    local_id=Path('/etc/machine-id').read_text().strip()
    addresses={a.get('address') for a in value.get('status',{}).get('addresses',[]) if a.get('type')=='InternalIP'}
    return (value.get('metadata', {}).get('name') == plan['node_name'] and local_id and info.get('machineID')==local_id
            and info.get('kubeletVersion')==plan['version'] and (not plan.get('node_ip') or plan['node_ip'] in addresses) and
            any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in value.get('status', {}).get('conditions', [])))


def verify_recovery():
    from node_state import status
    value=status()
    require(value['installed']['ready'],'k3s_recovery','Repair inconsistent registered resources before changing profiles')


def refresh_recovery(changed=(),allow_uninstalled=False):
    """Refresh exact resources after an explicitly applied managed profile."""
    path=Path('/etc/image-framework/k3s.json')
    require(path.is_file(),'k3s_policy','The K3s recovery policy is missing')
    policy=json.loads(path.read_text())
    plan=read_json(Path(policy['state_file']),{}).get('plan',{})
    if allow_uninstalled and not plan:return {'registered':False,'reason':'no_selected_k3s_installation'}
    require(plan and installed(policy)['ready'],'k3s_installation','Restore the selected K3s installation before changing profiles')
    from node_state import load,inventory
    data,_,catalog=load()
    missing,errors=inventory(Path('/'),data,catalog)
    allowed=set(changed)
    require(all(item['path'] in allowed for item in missing) and
            all(item['path'] in allowed and item['reason']=='resource_conflict' for item in errors),
            'k3s_recovery','Unrelated program/configuration resources changed during the profile operation')
    return capture(policy,plan)


def runtime(policy):
    state=read_json(Path(policy['state_file']),{})
    plan=state.get('plan',{})
    unit='k3s-agent.service' if plan.get('role')=='agent' else 'k3s.service'
    active=subprocess.run(['systemctl','is-active','--quiet',unit],capture_output=True,timeout=3).returncode==0
    cri=False;api=None
    if active:
        result=subprocess.run(['/usr/local/bin/k3s','crictl','--timeout=2s','info'],text=True,capture_output=True,timeout=4)
        if result.returncode==0:
            info=json.loads(result.stdout)
            cri=any(c.get('type')=='RuntimeReady' and c.get('status') is True for c in info.get('status',{}).get('conditions',[]))
        if plan.get('role')!='agent':
            result=subprocess.run(['/usr/local/bin/k3s','kubectl','--request-timeout=2s','get','--raw=/readyz'],
                                  text=True,capture_output=True,timeout=4)
            api=result.returncode==0 and result.stdout.strip()=='ok'
    return {'service':unit,'active':active,'cri_ready':cri,'api_ready':api,
            'ready':bool(plan and active and cri and api is not False)}


def mark_joined(policy, plan):
    if not cluster_ready(policy, plan):
        return False
    from auth_config import state, save, shadow_digest
    record = state()
    if not record.get('authentication') and not record.get('joined'):
        record.update(joined=True, reminder_pending=True, password_digest=shadow_digest())
        save(record)
    return True
