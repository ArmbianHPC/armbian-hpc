#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Interactive SSH setup for an online K3s installation."""

import argparse
from datetime import datetime, timezone
import errno
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None

POLICY_PATH = Path('/etc/image-framework/k3s.json')
RANCHER_CN_INSTALLER = 'https://rancher-mirror.rancher.cn/k3s/k3s-install.sh'
RANCHER_CN_ARTIFACTS = 'https://rancher-mirror.rancher.cn/k3s'
RANCHER_CN_INSTALLER_SHA256 = '3944aa467eb945b5ff2151a8e4f8d4a5f3a210d31ab39aec81f37606936d0863'
ROLES = {
    '1': ('server-single', '创建新集群：单控制节点（SQLite）'),
    '2': ('server-init', '创建新集群：初始化多控制节点方案（etcd）'),
    '3': ('agent', '加入已有集群：工作节点'),
    '4': ('server-join', '加入已有集群：控制节点'),
}


class SetupError(RuntimeError):
    pass


def run(args, *, capture=False, env=None, timeout=None):
    result = subprocess.run(args, text=True, capture_output=capture, env=env, timeout=timeout)
    if result.returncode:
        raise SetupError(f'{Path(args[0]).name} 执行失败，退出码 {result.returncode}。')
    return result.stdout.strip() if capture else ''


def ask(prompt, default=None, validator=None):
    while True:
        hint = f' [{default}]' if default is not None else ''
        value = input(prompt + hint + '：').strip()
        if not value and default is not None:
            value = str(default)
        try:
            if validator:
                validator(value)
            return value
        except (ValueError, SetupError) as exc:
            print(f'输入无效：{exc}')


def yes(prompt, default=False):
    value = ask(prompt + '（y/n）', 'y' if default else 'n', validate_yes)
    return value.lower() == 'y'


def validate_yes(value):
    if value.lower() not in ('y', 'n'):
        raise ValueError('请输入 y 或 n。')


def validate_name(value):
    if len(value) > 253 or not value:
        raise ValueError('节点名称长度应为 1 至 253。')
    for label in value.split('.'):
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label):
            raise ValueError('使用小写字母、数字和连字符组成节点名称。')


def validate_version(value):
    if not re.fullmatch(r'v\d+\.\d+\.\d+\+k3s\d+', value):
        raise ValueError('例如 v1.36.5+k3s1；加入集群时使用控制节点的版本。')


def validate_server(value):
    if any(character.isspace() for character in value):
        raise ValueError('集群地址不能包含空白字符。')
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname:
        raise ValueError('请输入 https://地址:端口。')
    if parsed.username is not None or parsed.password is not None:
        raise ValueError('集群地址不能包含用户名或密码。')
    if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('集群地址不能包含路径、查询参数或片段。')
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('端口应为 1 至 65535。')
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        validate_name(parsed.hostname.lower())


def validate_network(value):
    network = ipaddress.ip_network(value, strict=True)
    if network.version != 4:
        raise ValueError('首版引导使用 IPv4 集群网络。')


def validate_download_url(value):
    if not value or any(c.isspace() for c in value) or '\\' in value:
        raise ValueError('下载地址不能包含空白或反斜线。')
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError('请输入无凭据、无查询参数的 HTTPS 地址。')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('端口应为 1 至 65535。')


def validate_registry(value):
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?', value):
        raise ValueError('仓库名使用域名，可附带端口，例如 docker.io。')
    parsed = urllib.parse.urlsplit('https://' + value)
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('仓库端口无效。')


def validate_download_source(source):
    if not isinstance(source, dict) or set(source) != {'kind', 'installer_url', 'artifact_url', 'registry_mirrors'}:
        raise SetupError('保留的下载来源不完整，请重新选择来源。')
    if source['kind'] not in ('official', 'mirror'):
        raise SetupError('下载来源类型无效。')
    validate_download_url(source['installer_url'])
    validate_download_url(source['artifact_url'])
    if source['installer_url'] == RANCHER_CN_INSTALLER:
        if source['kind'] != 'mirror' or source['artifact_url'] != RANCHER_CN_ARTIFACTS:
            raise SetupError('Rancher 国内安装脚本需要使用配套的国内程序下载来源。')
    if not isinstance(source['registry_mirrors'], dict):
        raise SetupError('容器仓库配置无效。')
    for registry, endpoints in source['registry_mirrors'].items():
        validate_registry(registry)
        if not isinstance(endpoints, list) or not endpoints:
            raise SetupError('容器仓库缺少镜像站地址。')
        for endpoint in endpoints:
            validate_download_url(endpoint)


def collect_download_source(policy, previous=None):
    previous = previous or {}
    print('\n选择 K3s 下载来源：')
    print('  1. 上游官方安装脚本与 Release 下载地址')
    print('  2. 自定义镜像站（分别填写脚本地址和 Release 前缀）')
    print('  3. Rancher 中文社区国内源')
    default = '3' if previous.get('installer_url') == RANCHER_CN_INSTALLER else '2' if previous.get('kind') == 'mirror' else '1'
    choice = ask('下载来源', default,
                 lambda value: validate_source_choice(value))
    source = {'kind': 'official' if choice == '1' else 'mirror', 'registry_mirrors': {}}
    if choice == '1':
        source.update(installer_url=policy['installer_url'], artifact_url=policy['artifact_url'])
    elif choice == '3':
        source.update(installer_url=RANCHER_CN_INSTALLER, artifact_url=RANCHER_CN_ARTIFACTS)
        print('使用固定校验的国内安装脚本；K3s 版本与已有集群保持一致。')
    else:
        print('镜像站必须提供与固定 SHA256 一致的安装脚本，不能替换为修改过的脚本。')
        print('Release 前缀下需包含 <完整版本>/k3s-arm64 或 k3s-armhf 及对应 SHA256 文件。')
        source['installer_url'] = ask('安装脚本完整 HTTPS 地址', previous.get('installer_url'), validate_download_url)
        source['artifact_url'] = ask('Release 下载 HTTPS 前缀', previous.get('artifact_url'), validate_download_url).rstrip('/')
    if yes('配置容器仓库镜像站', bool(previous.get('registry_mirrors'))):
        default_registries = ','.join(previous.get('registry_mirrors', {})) or 'docker.io'
        registries = ask('需要映射的上游仓库，逗号分隔', default_registries, validate_registry_list)
        for registry in dict.fromkeys(item.strip() for item in registries.split(',')):
            old = previous.get('registry_mirrors', {}).get(registry, [])
            endpoint = ask(f'{registry} 的镜像站 HTTPS 地址', old[0] if old else None, validate_download_url)
            source['registry_mirrors'][registry] = [endpoint.rstrip('/')]
        print('已配置的仓库只使用指定镜像站；其他仓库按其原始地址下载。')
    validate_download_source(source)
    return source


def validate_source_choice(value):
    if value not in ('1', '2', '3'):
        raise ValueError('请输入 1、2 或 3。')


def installer_settings(policy, source):
    validate_download_source(source)
    return dict(policy, installer_url=source['installer_url'],
                installer_sha256=RANCHER_CN_INSTALLER_SHA256 if source['installer_url'] == RANCHER_CN_INSTALLER else policy['installer_sha256'])


def download_environment(source):
    validate_download_source(source)
    values = {'INSTALL_K3S_ARTIFACT_URL': source['artifact_url']}
    if source['installer_url'] == RANCHER_CN_INSTALLER:
        values.update(INSTALL_K3S_MIRROR='cn', INSTALL_K3S_MIRROR_URL='https://rancher-mirror.rancher.cn')
    return values


def validate_registry_list(value):
    for registry in value.split(','):
        validate_registry(registry.strip())


def registry_text(plan):
    mirrors = plan['download_source']['registry_mirrors']
    return json.dumps({'mirrors': {name: {'endpoint': endpoints} for name, endpoints in mirrors.items()}},
                      ensure_ascii=False, indent=2) + '\n'


def read_token():
    while True:
        token = getpass.getpass('加入 token（输入不回显）：').strip()
        if token and not any(character.isspace() for character in token):
            return token
        print('请输入有效的单行 token。')


def collect_server_options(previous=None):
    previous = previous or {}
    options = {}
    options['cluster-cidr'] = ask('Pod 网络', previous.get('cluster-cidr', '10.42.0.0/16'), validate_network)
    options['service-cidr'] = ask('Service 网络', previous.get('service-cidr', '10.43.0.0/16'), validate_network)
    pod_net = ipaddress.ip_network(options['cluster-cidr'])
    service_net = ipaddress.ip_network(options['service-cidr'])
    if pod_net.overlaps(service_net) or service_net.num_addresses <= 16:
        raise SetupError('Pod 与 Service 网络不能重叠；Service 网络至少需要 32 个地址。')
    dns = ask('集群 DNS 地址', previous.get('cluster-dns', str(service_net.network_address + 10)), ipaddress.IPv4Address)
    if ipaddress.ip_address(dns) not in service_net:
        raise SetupError('集群 DNS 地址必须属于 Service 网络。')
    options['cluster-dns'] = dns
    options['cluster-domain'] = ask('集群域名', previous.get('cluster-domain', 'cluster.local'), validate_name)
    options['flannel-backend'] = 'vxlan'
    options['secrets-encryption'] = yes('启用 Kubernetes Secret 加密', previous.get('secrets-encryption', False))
    options['disable'] = ask('需要关闭的内置组件，逗号分隔，留空使用默认配置', previous.get('disable', ''))
    options['tls-san'] = ask('API 证书额外地址或域名，逗号分隔，可留空', previous.get('tls-san', ''))
    return options


def private_write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SetupError(f'配置目标是符号链接：{path}')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_state(policy, state):
    state['updated_utc'] = datetime.now(timezone.utc).isoformat()
    private_write(policy['state_file'], json.dumps(state, ensure_ascii=False, indent=2) + '\n')


def collect_plan(policy, previous=None):
    previous = previous or {}
    print(f'\n{policy["display_name"]} / K3s 配置引导\n')
    for choice, (_, description) in ROLES.items():
        print(f'  {choice}. {description}')
    print('  0. 暂不配置，进入普通终端')
    default_role = next((key for key, value in ROLES.items() if value[0] == previous.get('role')), '0')
    choice = ask('选择节点用途', default_role, lambda value: validate_choice(value))
    if choice == '0':
        return None, None
    role = ROLES[choice][0]
    default_name = previous.get('node_name', socket.gethostname().lower())
    try:
        validate_name(default_name)
    except ValueError:
        default_name = 'node-01'
    plan = {
        'role': role,
        'node_name': ask('唯一节点名称', default_name, validate_name),
        'version': ask('K3s 完整版本（入群时与控制节点一致）', previous.get('version', policy['default_version']), validate_version),
        'server': '',
        'server_options': {},
    }
    token = None
    if role in ('agent', 'server-join'):
        plan['server'] = ask('集群入口，例如 https://192.168.1.10:6443', previous.get('server') or None, validate_server)
        token = read_token()
    if role != 'agent':
        if role == 'server-join':
            print('控制节点的集群参数必须与已有集群一致。')
        else:
            print('集群网络不能与局域网或其他集群网络重叠。')
        plan['server_options'] = collect_server_options(previous.get('server_options'))
    plan['download_source'] = collect_download_source(policy, previous.get('download_source'))
    return plan, token


def validate_choice(value):
    if value not in (*ROLES, '0'):
        raise ValueError('请输入菜单中的数字。')


def config_text(policy, plan):
    values = {'node-name': plan['node_name'], 'data-dir': policy['data_dir'],
              'private-registry': policy['configuration_dir'] + '/registries.yaml'}
    if plan['download_source']['registry_mirrors']:
        values['disable-default-registry-endpoint'] = True
    if plan['server']:
        values.update({'server': plan['server'], 'token-file': policy['configuration_dir'] + '/join-token'})
    if plan['role'] != 'agent':
        values['default-local-storage-path'] = policy['local_pv_dir']
        values.update(plan['server_options'])
        for key in ('disable', 'tls-san'):
            values[key] = [item.strip() for item in values[key].split(',') if item.strip()]
            if not values[key]:
                del values[key]
    if plan['role'] == 'server-init':
        values['cluster-init'] = True
    return ''.join(f'{key}: {json.dumps(value, ensure_ascii=False)}\n' for key, value in values.items())


def check_host(policy):
    if sys.platform != 'linux' or platform.machine() not in policy['runtime_machines']:
        raise SetupError(f'安装需要运行在此目标指定的 {policy["architecture"]} Linux 设备。')
    if not Path('/run/systemd/system').is_dir():
        raise SetupError('目标系统需要 systemd。')
    for command in ('dpkg', 'findmnt', 'mountpoint', 'mount', 'systemctl', 'curl', 'iptables', 'modprobe', 'sysctl'):
        if not shutil.which(command):
            raise SetupError(f'镜像缺少依赖：{command}')
    if run(['dpkg', '--print-architecture'], capture=True) != policy['architecture']:
        raise SetupError('用户空间架构与镜像目标不一致。')
    mountpoint = policy['data_mountpoint']
    mount = run(['findmnt', '-rn', '-M', mountpoint, '-o', 'TARGET,FSTYPE,LABEL'], capture=True).split()
    if mount != [mountpoint, policy['data_filesystem'], policy['data_label']]:
        raise SetupError(f'{mountpoint} 必须是标签正确的独立 {policy["data_filesystem"]} 数据分区。')
    if run(['findmnt', '-rn', '-M', '/', '-o', 'FSTYPE'], capture=True) != policy['root_filesystem']:
        raise SetupError(f'正常系统根文件系统必须使用 {policy["root_filesystem"]}。')
    controllers = Path('/sys/fs/cgroup/cgroup.controllers')
    if not controllers.exists() or 'memory' not in controllers.read_text().split():
        raise SetupError('目标镜像需要启用 cgroup v2 的 memory 控制器。')
    if any(line.split() and line.split()[0] != 'Filename' for line in Path('/proc/swaps').read_text().splitlines()):
        raise SetupError('请先关闭 swap，再运行配置引导。')


def ensure_bind(source, target, fstab_lines, data_mountpoint):
    source, target = Path(source), Path(target)
    source.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    if source.is_symlink() or target.is_symlink():
        raise SetupError(f'绑定目录不能是符号链接：{target}')
    if subprocess.run(['mountpoint', '-q', str(target)]).returncode == 0:
        if not os.path.samefile(source, target):
            raise SetupError(f'{target} 已挂载其他目录。')
    else:
        if any(target.iterdir()):
            raise SetupError(f'{target} 已有未迁移的数据，请先处理已有安装。')
        run(['mount', '--bind', str(source), str(target)])
    matches = [line for line in fstab_lines if line.strip() and not line.lstrip().startswith('#') and len(line.split()) > 1 and line.split()[1] == str(target)]
    if matches:
        if any(len(line.split()) < 4 or line.split()[0] != str(source) or 'bind' not in line.split()[3].split(',') for line in matches):
            raise SetupError(f'fstab 中 {target} 已有其他配置。')
    else:
        fstab_lines.append(f'{source} {target} none bind,x-systemd.requires-mounts-for={data_mountpoint} 0 0')


def valid_data_entry(fields, policy, mounted_uuid):
    sources = {'LABEL=' + policy['data_label']}
    if mounted_uuid:
        sources.add('UUID=' + mounted_uuid)
    return len(fields) >= 4 and fields[0] in sources and fields[2] == policy['data_filesystem']


def prepare_storage(policy, plan):
    config_dir = Path(policy['configuration_dir'])
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.chmod(0o700)
    for path in (policy['data_dir'], policy['kubelet_source'], policy['local_pv_dir'], policy['logs_dir'], policy['backups_dir']):
        Path(path).mkdir(parents=True, exist_ok=True)
    fstab = Path('/etc/fstab')
    lines = fstab.read_text().splitlines()
    mountpoint = policy['data_mountpoint']
    data_entries = [line for line in lines if line.strip() and not line.lstrip().startswith('#') and len(line.split()) > 1 and line.split()[1] == mountpoint]
    mounted_uuid = run(['findmnt', '-rn', '-M', mountpoint, '-o', 'UUID'], capture=True)
    for entry in data_entries:
        fields = entry.split()
        if not valid_data_entry(fields, policy, mounted_uuid):
            raise SetupError(f'fstab 中 {mountpoint} 与预期数据分区不一致。')
    if not data_entries:
        lines.append(f"LABEL={policy['data_label']} {mountpoint} {policy['data_filesystem']} defaults,noatime 0 2")
    ensure_bind(policy['kubelet_source'], policy['kubelet_target'], lines, mountpoint)
    ensure_bind(policy['node_identity_source'], policy['node_identity_target'], lines, mountpoint)
    private_write('/etc/fstab', '\n'.join(lines) + '\n', 0o644)
    config_dir.joinpath('node').chmod(0o700)
    private_write(config_dir / 'config.yaml', config_text(policy, plan))
    private_write(config_dir / 'registries.yaml', registry_text(plan))
    private_write('/etc/sysctl.d/90-k3s.conf', 'net.ipv4.ip_forward=1\nnet.bridge.bridge-nf-call-iptables=1\nnet.bridge.bridge-nf-call-ip6tables=1\n', 0o644)
    run(['modprobe', 'overlay'])
    run(['modprobe', 'br_netfilter'])
    run(['modprobe', 'vxlan'])
    run(['sysctl', '-p', '/etc/sysctl.d/90-k3s.conf'])
    service = 'k3s-agent' if plan['role'] == 'agent' else 'k3s'
    dropin = Path('/etc/systemd/system') / (service + '.service.d/10-image-framework-storage.conf')
    required_mounts = ' '.join((mountpoint, policy['kubelet_target'], policy['node_identity_target']))
    private_write(dropin, f'[Unit]\nWants=network-online.target\nAfter=network-online.target\nRequiresMountsFor={required_mounts}\n\n[Service]\nExecStartPre=/usr/local/sbin/armhpc-kube check\n', 0o644)
    return service


def download_installer(policy, destination):
    destination = Path(destination)
    for attempt in range(1, 4):
        temporary = None
        try:
            digest = hashlib.sha256()
            request = urllib.request.Request(policy['installer_url'], headers={'User-Agent': 'Image-Framework-Setup/1'})
            print(f'下载安装脚本（{attempt}/3）：{policy["installer_url"]}', flush=True)
            with urllib.request.urlopen(request, timeout=60) as response:
                validate_download_url(response.geturl())
                with tempfile.NamedTemporaryFile(prefix='.install-', dir=destination.parent, delete=False) as output:
                    temporary = Path(output.name)
                    total = 0
                    while block := response.read(65536):
                        total += len(block)
                        if total > 1024 * 1024:
                            raise SetupError('安装脚本超过预期大小。')
                        digest.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
            if digest.hexdigest() != policy['installer_sha256']:
                raise SetupError('在线安装脚本的 SHA256 与固定版本不一致；未执行脚本。')
            os.replace(temporary, destination)
            temporary = None
            print(f'安装脚本已校验：{total} 字节。', flush=True)
            return
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            reason = getattr(error, 'reason', error)
            retryable = (isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError)) or
                         getattr(error, 'errno', None) in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT))
            retryable = retryable and not isinstance(reason, ssl.SSLCertVerificationError)
            if isinstance(error, urllib.error.HTTPError):
                retryable = error.code in (408, 429, 500, 502, 503, 504)
            if not retryable or attempt == 3:
                raise SetupError(f'安装脚本下载失败：{policy["installer_url"]}；{error}。已保留节点配置，可重试或修改下载来源。') from error
            delay = (1, 3)[attempt - 1]
            print(f'网络下载未完成：{error}；{delay} 秒后重试。', flush=True)
            time.sleep(delay)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def wait_for_service(policy, service):
    deadline = time.monotonic() + policy.get('service_start_timeout_seconds', 180)
    print('等待 K3s 服务启动……')
    while time.monotonic() < deadline:
        active = run(['systemctl', 'show', service, '--property=ActiveState', '--value'], capture=True, timeout=10)
        if active == 'active':
            return
        if active == 'failed':
            raise SetupError(f'{service} 启动失败，请查看 journalctl 日志。')
        time.sleep(2)
    run(['systemctl', 'stop', '--no-block', service], timeout=10)
    raise SetupError(f'{service} 启动超时。已保留配置，可检查网络后重试。')


def install(policy, state, token):
    plan = state['plan']
    source = plan['download_source']
    validate_download_source(source)
    state['status'] = 'installing'
    save_state(policy, state)
    service = prepare_storage(policy, plan)
    if token is not None:
        private_write(Path(policy['configuration_dir']) / 'join-token', token + '\n')
    if plan['server'] and not Path(policy['configuration_dir'], 'join-token').is_file():
        raise SetupError('保留的加入凭据缺失，请重新提供配置。')
    print('\n下载并校验安装脚本，随后下载 K3s 程序。容器镜像由 containerd 在线拉取。')
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith(('K3S_', 'INSTALL_K3S_')) or key == 'ARCH':
            del environment[key]
    environment.update({
        'INSTALL_K3S_VERSION': plan['version'],
        'INSTALL_K3S_EXEC': 'agent' if plan['role'] == 'agent' else 'server',
        'INSTALL_K3S_SKIP_START': 'true',
        'K3S_CONFIG_FILE': policy['configuration_dir'] + '/config.yaml',
        'ARCH': policy['installer_arch'],
    })
    environment.update(download_environment(source))
    with tempfile.TemporaryDirectory(prefix='armhpc-k3s-configuration-', dir='/run') as directory:
        installer = Path(directory) / 'install.sh'
        download_installer(installer_settings(policy, source), installer)
        run(['sh', str(installer)], env=environment, timeout=900)
    state['status'] = 'starting'
    save_state(policy, state)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', service])
    run(['systemctl', 'restart', '--no-block', service])
    wait_for_service(policy, service)
    run(['systemctl', 'is-active', '--quiet', service])
    state['status'] = 'installed'
    save_state(policy, state)
    private_write(policy['completion_marker'], 'configured\n', 0o644)
    print('\nK3s 已安装，服务已启动。')
    if not plan['server']:
        print(f"工作节点加入凭据文件：{policy['data_dir']}/server/agent-token")
        print(f"控制节点加入凭据文件：{policy['data_dir']}/server/token")
    print('请在控制节点执行 kubectl get nodes，确认此节点达到 Ready。')
    print(f'查看本机日志：sudo journalctl -u {service} -n 80 --no-pager')


def main():
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG'), description='通过 SSH 配置并联网安装 K3s。')
    parser.add_argument('--auto', action='store_true', help='由未配置的交互式 SSH 登录触发。')
    parser.add_argument('--preview', action='store_true', help='只预览配置，不下载或安装。')
    parser.add_argument('--policy', type=Path, default=POLICY_PATH, help='预览时可指定策略文件。')
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SetupError('请使用交互式 SSH 终端。引导不会接管非交互会话。')
    if args.policy != POLICY_PATH and not args.preview:
        raise SetupError('自定义策略路径仅用于预览。')
    policy = json.loads(args.policy.read_text())
    if not args.preview:
        management = Path(policy["backups_dir"]) / "k3s-management.json"
        if management.is_file() and json.loads(management.read_text()).get("stage") == "incomplete":
            raise SetupError("K3s 管理尚未完成；请继续原 uninstall、reinstall 或 clear 操作后再安装。")
    if not args.preview and os.geteuid() != 0:
        os.execvp('sudo', ['sudo', '--', '/usr/local/sbin/armhpc-kube', 'configuration', *sys.argv[1:]])
    if args.auto and Path(policy['completion_marker']).exists() and os.access('/usr/local/bin/k3s', os.X_OK):
        return
    lock = None
    state = None
    if not args.preview:
        check_host(policy)
        lock = open('/run/image-framework-armhpc-k3s-configuration.lock', 'w')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('已有配置引导正在运行。')
        state_path = Path(policy['state_file'])
        if state_path.exists():
            state = json.loads(state_path.read_text())
    if state:
        plan, token = state['plan'], None
        print('\n发现保留的节点配置。修复使用记录的角色、名称和版本，并保留数据。')
        print('  1. 使用已有配置修复或重试')
        print('  2. 修正集群地址、凭据、网络参数或下载来源')
        print('  0. 返回普通终端')
        action = ask('选择操作', '0', lambda value: validate_repair_choice(value))
        if action == '0':
            return
        if action == '2':
            plan = json.loads(json.dumps(plan))
            data_dir = Path(policy['data_dir'])
            if not data_dir.exists() or not any(data_dir.iterdir()):
                plan, token = collect_plan(policy, plan)
                if plan is None:
                    return
            else:
                if plan['server']:
                    plan['server'] = ask('集群入口', plan['server'], validate_server)
                    if yes('重新输入加入 token', False):
                        token = read_token()
                if plan['role'] != 'agent':
                    plan['server_options'] = collect_server_options(plan['server_options'])
                plan['download_source'] = collect_download_source(policy, plan.get('download_source'))
            state['plan'] = plan
        if 'download_source' not in plan:
            plan['download_source'] = collect_download_source(policy)
    else:
        data_dir = Path(policy['data_dir'])
        if not args.preview and data_dir.exists() and any(data_dir.iterdir()):
            raise SetupError('发现已有 K3s 数据，但缺少引导记录。请先核对已有安装。')
        plan, token = collect_plan(policy)
        if plan is None:
            return
        state = {'schema_version': 1, 'operation_id': str(uuid.uuid4()), 'status': 'planned', 'plan': plan}
    validate_download_source(plan['download_source'])
    print('\n即将使用的配置（token 不显示）：')
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    print(f'系统分区：{policy["root_filesystem"]} / {policy["root_size_bytes"] / 1024**3:g} GiB；K3s 数据目录：{policy["data_dir"]}。')
    if args.preview:
        print('\n预览结束。没有下载或安装。')
        return
    if not yes('开始联网安装或修复', False):
        return
    marker = Path(policy['completion_marker'])
    marker.unlink(missing_ok=True)
    try:
        install(policy, state, token)
    except Exception:
        state['status'] = 'failed'
        save_state(policy, state)
        raise
    finally:
        if lock:
            lock.close()


def validate_repair_choice(value):
    if value not in ('0', '1', '2'):
        raise ValueError('请输入菜单中的数字。')


if __name__ == '__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('\n引导已退出。可重新运行 armhpc configuration。')
        sys.exit(130)
    except Exception as exc:
        print(f'\n配置未完成：{exc}', file=sys.stderr)
        print('检查网络和系统日志后，重新运行 sudo armhpc configuration。', file=sys.stderr)
        sys.exit(1)
