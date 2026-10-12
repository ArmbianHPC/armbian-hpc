#!/usr/bin/python3
# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Manage IPv4 with systemd-networkd, explicit confirmation and timed rollback."""
import argparse
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_io import (ConfigError, approve, atomic_write, configuration_lock, emit, entry,
                       normal_system, read_json, require, require_idle, run, safe_path, save_json, sync_directory)

STATE = 'var/lib/armhpc/network-config/transaction.json'
HEADER = '# Managed by armhpc network\n'
PENDING = {'queued', 'applying', 'pending', 'rollback-failed'}


def monotonic():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def validate_interface(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,14}', value) and value != 'lo',
            'interface_invalid', 'Use a physical interface name, for example eth0')
    return value


def settings(interface, dhcp=False, address=None, gateway=None, dns=None):
    validate_interface(interface)
    require(bool(dhcp) != bool(address), 'network_arguments', 'Select --dhcp or --address IPv4/PREFIX')
    parsed = None
    if address:
        require('/' in address, 'address_invalid', 'Static IPv4 requires a prefix, for example 192.168.100.20/24')
        try:
            parsed = ipaddress.IPv4Interface(address)
        except ValueError as error:
            raise ConfigError('address_invalid', str(error)) from error
        require(not (parsed.ip.is_unspecified or parsed.ip.is_multicast or parsed.ip.is_loopback or parsed.ip.is_link_local),
                'address_invalid', 'Static IPv4 must be a usable unicast address')
        require(parsed.network.prefixlen >= 31 or parsed.ip not in (parsed.network.network_address, parsed.network.broadcast_address),
                'address_invalid', 'Network and broadcast addresses are not host addresses')
    if gateway:
        require(parsed is not None, 'gateway_invalid', '--gateway is available only with a static address')
        route = ipaddress.IPv4Address(gateway)
        require(route in parsed.network and route != parsed.ip and not (route.is_unspecified or route.is_multicast or route.is_loopback) and
                (parsed.network.prefixlen >= 31 or route not in (parsed.network.network_address, parsed.network.broadcast_address)),
                'gateway_invalid', 'Gateway must be a different usable address on the configured subnet')
        gateway = str(route)
    servers = []
    for item in dns or ():
        for value in item.split(','):
            parsed_dns = ipaddress.ip_address(value.strip())
            require(not (parsed_dns.is_unspecified or parsed_dns.is_multicast or parsed_dns.is_link_local),
                    'dns_invalid', 'DNS servers must be usable IP addresses')
            if str(parsed_dns) not in servers:
                servers.append(str(parsed_dns))
    require(len(servers) <= 6, 'dns_invalid', 'Use at most six DNS servers')
    return {'interface': interface, 'method': 'dhcp' if dhcp else 'static',
            'address': str(parsed) if parsed else None, 'gateway': gateway, 'dns': servers}


def render(value):
    dns_auto = 'no' if value['dns'] else 'yes'
    rows = [HEADER.rstrip(), '[Match]', 'Name=' + value['interface'], '', '[Network]',
            'DHCP=' + ('yes' if value['method'] == 'dhcp' else 'ipv6'),
            'LinkLocalAddressing=ipv6', 'IPv6AcceptRA=yes']
    if value['address']:
        rows.append('Address=' + value['address'])
    if value['gateway']:
        rows.append('Gateway=' + value['gateway'])
    rows.extend('DNS=' + address for address in value['dns'])
    for section in ('DHCPv4', 'DHCPv6', 'IPv6AcceptRA'):
        rows.extend(('', '[' + section + ']', 'UseDNS=' + dns_auto))
        if section != 'IPv6AcceptRA':
            rows.append('UseHostname=no')
    return '\n'.join(rows) + '\n'


class Network:
    def __init__(self, root=Path('/')):
        self.root = Path(root).resolve()
        self.live = self.root == Path('/')
        self.state = safe_path(self.root, STATE)

    def boot_id(self):
        return (self.root / 'proc/sys/kernel/random/boot_id').read_text().strip()

    def interfaces(self):
        directory = self.root / 'sys/class/net'
        return sorted(p.name for p in directory.glob('*') if p.name != 'lo' and (p / 'device').exists())

    def choose_interface(self, interface=None):
        available = self.interfaces()
        if not interface:
            require(len(available) == 1, 'interface_required', 'Select --interface; available interfaces: ' + ', '.join(available))
            interface = available[0]
        validate_interface(interface)
        require(interface in available, 'interface_missing', 'Physical interface does not exist: ' + interface)
        require(not (self.root / 'sys/class/net' / interface / 'master').exists(), 'interface_managed', 'Configure bridge or bond member interfaces with their network manager')
        return interface

    def config_path(self, interface):
        return safe_path(self.root, 'etc/systemd/network/00-armhpc-' + validate_interface(interface) + '.network')

    def record(self):
        value = read_json(self.state)
        if value:
            try:
                require(value.get('protocol') == 1 and str(uuid.UUID(value['transaction_id'])) == value['transaction_id'],
                        'configuration_state', 'Invalid network transaction identity')
                proposed = value['proposed']
                require(settings(proposed['interface'], proposed['method'] == 'dhcp', proposed['address'],
                                 proposed['gateway'], proposed['dns']) == proposed,
                        'configuration_state', 'Network transaction contains invalid settings')
                require(value['stage'] in PENDING | {'confirmed', 'rolled-back', 'failed'} and
                        isinstance(value['boot_id'], str) and bool(value['boot_id']) and
                        isinstance(value['rollback_after'], int) and 30 <= value['rollback_after'] <= 600,
                        'configuration_state', 'Invalid network transaction state')
                require(hashlib.sha256(render(proposed).encode()).hexdigest() == value['sha256'],
                        'configuration_state', 'Network transaction settings/checksum differ')
                if 'deadline' in value:
                    require(isinstance(value['deadline'], (int, float)) and math.isfinite(value['deadline']),
                            'configuration_state', 'Network transaction deadline is invalid')
                require(value['stage'] not in ('applying', 'pending') or 'deadline' in value,
                        'configuration_state', 'Pending network transaction has no deadline')
                backup = value['backup']
                require(backup is None or isinstance(backup, dict) and isinstance(backup.get('content'), str) and
                        backup['content'].startswith(HEADER) and isinstance(backup.get('mode'), int) and 0 <= backup['mode'] <= 0o777,
                        'configuration_state', 'Network transaction backup is invalid')
            except (ValueError, TypeError, KeyError) as error:
                raise ConfigError('configuration_state', 'Malformed network transaction record') from error
        return value

    def public(self, record):
        if not record:
            return None
        result = {key: record[key] for key in ('transaction_id', 'stage', 'boot_id', 'proposed', 'rollback_after')}
        if 'error' in record:
            result['error'] = record['error']
        result['can_confirm'] = record['stage'] == 'pending' and record['boot_id'] == self.boot_id()
        result['confirm_command'] = 'armhpc network confirm --transaction ' + record['transaction_id'] + ' --yes'
        if 'deadline' in record and record['boot_id'] == self.boot_id():
            result['seconds_remaining'] = max(0, round(record['deadline'] - monotonic(), 1))
        return result

    def status(self, interface=None):
        if interface:
            interface = self.choose_interface(interface)
        rows = []
        for name in ([interface] if interface else self.interfaces()):
            base = self.root / 'sys/class/net' / name
            def read(field):
                try:
                    return (base / field).read_text().strip()
                except OSError:
                    return None
            path = self.config_path(name)
            rows.append({'interface': name, 'mac': read('address'), 'carrier': read('carrier'),
                         'managed_config': str(path) if path.is_file() else None})
        data = {'backend': 'systemd-networkd', 'interfaces': rows, 'transaction': self.public(self.record())}
        if self.live:
            data['addresses'] = json.loads(run('ip', '-j', 'address', 'show', *(['dev', interface] if interface else [])))
            data['ipv4_routes'] = json.loads(run('ip', '-j', '-4', 'route', 'show', *(['dev', interface] if interface else [])))
            data['networkd_active'] = self.active('systemd-networkd.service')
        return data

    def active(self, service):
        return subprocess.run(['systemctl', 'is-active', '--quiet', service], capture_output=True, timeout=5).returncode == 0

    def preflight(self, value):
        normal_system(self.root)
        self.choose_interface(value['interface'])
        if self.live:
            require(self.active('systemd-networkd.service'), 'network_backend', 'systemd-networkd is not active; the command does not take over other network managers')
            if self.active('NetworkManager.service'):
                state = run('nmcli', '-g', 'GENERAL.STATE', 'device', 'show', value['interface'])
                require(state.startswith('10 '), 'network_backend', 'NetworkManager manages this interface; use its configuration commands')
            if self.active('networking.service'):
                ifstate = self.root / 'run/network/ifstate'
                require(not ifstate.is_file() or not any(line.split('=', 1)[0] == value['interface'] for line in ifstate.read_text().splitlines()),
                        'network_backend', 'ifupdown manages this interface; use its configuration commands')
            if value['dns']:
                require(self.active('systemd-resolved.service'), 'dns_backend', 'Custom DNS requires an active systemd-resolved service')
                resolv = Path('/etc/resolv.conf')
                require(resolv.is_symlink() and resolv.resolve() in (Path('/run/systemd/resolve/stub-resolv.conf'), Path('/run/systemd/resolve/resolv.conf')),
                        'dns_backend', '/etc/resolv.conf is not connected to systemd-resolved')
            info = run('networkctl', 'status', '--no-pager', '--full', '-n', '0', value['interface'])
            match = re.search(r'Network File:\s+(\S+)', info)
            if match and match[1] not in ('n/a', '-'):
                require(Path(match[1]).name >= self.config_path(value['interface']).name, 'network_priority',
                        'An earlier network configuration owns this interface; resolve its priority before applying')
                current = Path(match[1]).read_text()
                require(not re.search(r'^\s*(Bridge|Bond|VLAN|VRF|VXLAN|MACVLAN|IPVLAN|Tunnel)\s*=', current, re.M),
                        'network_advanced', 'This interface has bridge, VLAN or tunnel configuration; manage that configuration directly')
        path = self.config_path(value['interface'])
        if path.exists():
            require(path.is_file() and path.read_text().startswith(HEADER), 'network_config_owned', 'Managed filename is occupied by an unrelated configuration')

    def plan(self, value, rollback_after=90):
        require(30 <= rollback_after <= 600, 'rollback_timeout', 'Rollback timeout must be between 30 and 600 seconds')
        self.preflight(value)
        return {'backend': 'systemd-networkd', 'settings': value, 'config_file': str(self.config_path(value['interface'])),
                'content': render(value), 'rollback_after': rollback_after, 'confirmation_required': True}

    def submit(self, value, rollback_after=90):
        with configuration_lock(self.root):
            require_idle(self.root)
            plan = self.plan(value, rollback_after)
            old = self.record()
            require(not old or old['stage'] not in PENDING, 'network_pending', 'Confirm or roll back the current network transaction first')
            path = self.config_path(value['interface'])
            previous = {'content': path.read_text(), 'mode': path.stat().st_mode & 0o777} if path.exists() else None
            record = {'protocol': 1, 'transaction_id': str(uuid.uuid4()), 'boot_id': self.boot_id(),
                      'stage': 'queued', 'proposed': value, 'rollback_after': rollback_after,
                      'backup': previous, 'sha256': hashlib.sha256(plan['content'].encode()).hexdigest()}
            self.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.state.parent.chmod(0o700)
            save_json(self.state, record)
            try:
                if self.live:
                    run('systemd-run', '--quiet', '--unit=armhpc-network-' + record['transaction_id'],
                        '--property=Type=exec', '--property=Restart=on-failure', '--property=RestartSec=2',
                        '--property=RuntimeMaxSec=' + str(rollback_after + 60),
                        '/usr/bin/python3', Path(__file__).resolve(), '_service', '--transaction', record['transaction_id'])
            except Exception as error:
                record.update(stage='failed', error=str(error))
                save_json(self.state, record)
                raise
            return self.public(record)

    def selected(self, identifier=None):
        record = self.record()
        require(record is not None and (not identifier or record['transaction_id'] == identifier),
                'network_transaction', 'Network transaction not found or ID differs')
        return record

    def reload(self, interface):
        if self.live:
            run('networkctl', 'reload')
            run('networkctl', 'reconfigure', interface)

    def apply(self, identifier):
        with configuration_lock(self.root):
            record = self.selected(identifier)
            require(record['boot_id'] == self.boot_id(), 'network_boot', 'A pending transaction cannot be applied after a reboot')
            if record['stage'] != 'queued':
                return self.public(record)
            try:
                require_idle(self.root)
                self.preflight(record['proposed'])
                require(not record['backup'] or self.config_path(record['proposed']['interface']).read_text() == record['backup']['content'],
                        'network_config_changed', 'Managed configuration changed after submission')
                require(record['backup'] or not self.config_path(record['proposed']['interface']).exists(),
                        'network_config_changed', 'Managed configuration appeared after submission')
            except Exception as error:
                record.update(stage='failed', error=str(error))
                save_json(self.state, record)
                return self.public(record)
            record.update(stage='applying', deadline=monotonic() + record['rollback_after'])
            save_json(self.state, record)
            try:
                atomic_write(self.config_path(record['proposed']['interface']), render(record['proposed']))
                self.reload(record['proposed']['interface'])
                record['stage'] = 'pending'
                save_json(self.state, record)
            except Exception as error:
                record['error'] = str(error)
                self.restore(record)
            return self.public(record)

    def restore(self, record, reload=True):
        path = self.config_path(record['proposed']['interface'])
        if record['stage'] == 'queued':
            record['stage'] = 'rolled-back'
            save_json(self.state, record)
            return
        try:
            backup = record['backup']
            current = path.read_text() if path.exists() else None
            require(current is None or hashlib.sha256(current.encode()).hexdigest() == record['sha256'] or
                    (backup and current == backup['content']), 'network_config_changed', 'Network file changed outside this transaction; automatic rollback refused')
            if backup:
                require(backup['content'].startswith(HEADER), 'configuration_state', 'Invalid managed-configuration backup')
                atomic_write(path, backup['content'], backup['mode'])
            elif path.exists():
                path.unlink()
                sync_directory(path.parent)
            record['stage'] = 'rolled-back'
            save_json(self.state, record)
            if reload:
                self.reload(record['proposed']['interface'])
        except Exception as error:
            record.update(stage='rollback-failed', error=str(error))
            save_json(self.state, record)
            raise

    def rollback(self, identifier=None, reload=True):
        normal_system(self.root)
        with configuration_lock(self.root):
            record = self.selected(identifier)
            require(record['stage'] in PENDING or record['stage'] == 'confirmed', 'network_transaction', 'This transaction is already finished')
            self.restore(record, reload)
            return self.public(record)

    def confirm(self, identifier=None):
        normal_system(self.root)
        with configuration_lock(self.root):
            record = self.selected(identifier)
            require(record['stage'] == 'pending' and record['boot_id'] == self.boot_id(), 'network_confirmation', 'Wait for live application, then confirm from the new connection')
            require(monotonic() < record['deadline'], 'network_expired', 'Confirmation deadline has expired; await rollback')
            path = self.config_path(record['proposed']['interface'])
            require(hashlib.sha256(path.read_bytes()).hexdigest() == record['sha256'], 'network_config_changed', 'Applied configuration changed')
            base = self.root / 'sys/class/net' / record['proposed']['interface']
            require((base / 'carrier').read_text().strip() == '1', 'network_link', 'Interface has no physical link')
            if self.live:
                addresses = json.loads(run('ip', '-j', '-4', 'address', 'show', 'dev', record['proposed']['interface']))
                local = [item for row in addresses for item in row.get('addr_info', []) if item.get('scope') == 'global']
                wanted = record['proposed']['address']
                require(any(not wanted or (item['local'] + '/' + str(item['prefixlen'])) == wanted for item in local),
                        'network_address', 'Configured IPv4 is not active; rollback remains armed')
            record.update(stage='confirmed', confirmed_monotonic=monotonic())
            save_json(self.state, record)
            return self.public(record)

    def expire(self, identifier):
        with configuration_lock(self.root):
            record = self.record()
            if not record or record['transaction_id'] != identifier or record['stage'] not in PENDING:
                return False
            if record['boot_id'] != self.boot_id() or monotonic() >= record.get('deadline', 0):
                self.restore(record)
                return False
            return True

    def recover_on_boot(self):
        if not (self.root / 'etc/armhpc/rescue-environment').is_file():
            with configuration_lock(self.root):
                record = self.record()
                if record and record['stage'] in PENDING:
                    self.restore(record, reload=False)


def serve(network, identifier, delay=2):
    time.sleep(delay)
    record = network.record()
    if not record or record['transaction_id'] != identifier:
        return
    network.apply(identifier)
    while network.expire(identifier):
        time.sleep(1)


def login_prompt(network, stdin=None, stdout=None):
    """Offer one bounded decision without treating login as confirmation."""
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    if (network.root / 'etc/armhpc/rescue-environment').is_file():
        return 0
    record = network.record()
    if not record or record['stage'] not in ('queued', 'applying', 'pending'):
        return 0
    identifier = record['transaction_id']
    ready_deadline = monotonic() + 3
    while record['stage'] in ('queued', 'applying') and monotonic() < ready_deadline:
        time.sleep(0.1)
        record = network.record()
        if not record or record['transaction_id'] != identifier or record['stage'] not in ('queued', 'applying', 'pending'):
            return 0
    if record['boot_id'] != network.boot_id() or record['stage'] != 'pending':
        print('网络配置正在应用或恢复；请在终端使用 armhpc network show 查询。', file=stdout, flush=True)
        return 10
    remaining = record['deadline'] - monotonic()
    if remaining <= 0:
        print('网络配置确认期限已到，后台服务将恢复旧配置。', file=stdout, flush=True)
        return 10
    proposed = record['proposed']
    print('\nARMHPC 网络配置待确认', file=stdout)
    print('网口：' + proposed['interface'] + '；IPv4：' + (proposed['address'] or 'DHCP'), file=stdout)
    if proposed['gateway']:
        print('网关：' + proposed['gateway'], file=stdout)
    if proposed['dns']:
        print('DNS：' + ', '.join(proposed['dns']), file=stdout)
    print('登录不会自动保存配置。超过期限未确认将恢复旧配置。', file=stdout)
    command = network.public(record)['confirm_command']
    try:
        while True:
            remaining = record['deadline'] - monotonic()
            if remaining <= 0:
                print('\n确认已超时，后台服务将恢复旧配置。', file=stdout, flush=True)
                return 10
            stdout.write(f'剩余约 {math.ceil(remaining)} 秒：[y] 确认保存 / [r] 回滚 / [Enter] 稍后确认：')
            stdout.flush()
            while True:
                current = network.record()
                if not current or current['transaction_id'] != identifier:
                    print('\n网络事务已变化，本次提示未执行操作。', file=stdout, flush=True)
                    return 10
                if current['stage'] != 'pending':
                    message = {'confirmed': '此网络配置已经确认。', 'rolled-back': '此网络配置已经回滚。'}.get(
                        current['stage'], '网络事务状态已变化，请使用 armhpc network show 查询。')
                    print('\n' + message, file=stdout, flush=True)
                    return 10
                remaining = record['deadline'] - monotonic()
                if remaining <= 0:
                    print('\n确认已超时，后台服务将恢复旧配置。', file=stdout, flush=True)
                    return 10
                ready, _, _ = select.select([stdin], [], [], min(remaining, 1))
                if ready:
                    break
            line = stdin.readline()
            answer = line.strip().lower()
            if not line or answer in ('', 'n', 'no', '0'):
                print('暂不确认；后台回滚倒计时继续。\n手动确认：' + command, file=stdout, flush=True)
                return 10
            if answer in ('y', 'yes', '1'):
                network.confirm(identifier)
                print('新网络配置已确认并保存。', file=stdout, flush=True)
                return 10
            if answer in ('r', 'rollback', '2'):
                network.rollback(identifier)
                print('已恢复旧网络配置。', file=stdout, flush=True)
                return 10
            print('请输入 y、r，或直接按 Enter。', file=stdout, flush=True)
    except (KeyboardInterrupt, EOFError):
        print('\n暂不确认；后台回滚倒计时继续。\n手动确认：' + command, file=stdout, flush=True)
        return 10
    except (ConfigError, OSError) as error:
        print('\n本次确认未完成：' + str(error) + '\n请查询网络事务后继续操作：' + command, file=stdout, flush=True)
        return 10


def wizard(network, interface=None):
    require(sys.stdin.isatty(), 'interactive_required', 'Run the network wizard from an interactive terminal')
    choices = network.interfaces()
    require(bool(choices), 'interface_missing', 'No physical network interface found')
    selected = interface or (choices[0] if len(choices) == 1 else input('网口 (' + ', '.join(choices) + '): ').strip())
    method = input('IPv4 获取方式 [dhcp/static] (dhcp): ').strip().lower() or 'dhcp'
    require(method in ('dhcp', 'static'), 'network_arguments', 'Select dhcp or static')
    address = input('IPv4 地址/前缀，例如 192.168.100.20/24: ').strip() if method == 'static' else None
    gateway = (input('IPv4 网关（可留空）: ').strip() or None) if method == 'static' else None
    dns = input('DNS 地址，以逗号分隔（留空使用自动 DNS）: ').strip()
    return settings(selected, method == 'dhcp', address, gateway, [dns] if dns else [])


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == '_login':
        require(len(arguments) == 1, 'arguments', 'Login confirmation takes no arguments')
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return 0
        normal_system(Path('/'))
        return login_prompt(Network())
    if arguments and arguments[0] == '_service':
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument('action')
        parser.add_argument('--transaction', required=True)
        args = parser.parse_args(arguments)
        normal_system(Path('/'))
        serve(Network(), args.transaction)
        return 0
    parser = argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG', 'armhpc network'), description=__doc__)
    parser.add_argument('action', nargs='?', default='show', choices=('show', 'set', 'configure', 'confirm', 'rollback'))
    parser.add_argument('--interface')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--dhcp', action='store_true')
    group.add_argument('--address', help='Static IPv4 with prefix length.')
    parser.add_argument('--gateway')
    parser.add_argument('--dns', action='append', help='DNS IP; repeat or use a comma-separated list.')
    parser.add_argument('--rollback-after', type=int, default=90, help='Confirmation window in seconds (30–600).')
    parser.add_argument('--transaction', help='Network transaction ID to confirm or roll back.')
    parser.add_argument('--plan', action='store_true', help='Preview without changing configuration.')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(arguments)
    require(not (args.dhcp or args.address or args.gateway or args.dns or args.plan) or args.action == 'set',
            'arguments', 'Network settings and --plan are accepted only by set')
    require(args.transaction is None or args.action in ('confirm', 'rollback'), 'arguments', '--transaction is accepted only by confirm or rollback')
    require(args.rollback_after == 90 or args.action in ('set', 'configure'), 'arguments', '--rollback-after is accepted only by set or configure')
    network = Network()
    if args.action == 'show':
        result = network.status(args.interface)
    elif args.action in ('confirm', 'rollback'):
        require(not args.interface, 'arguments', 'The transaction already binds its interface')
        approve(args.yes, '确认新网络配置？' if args.action == 'confirm' else '恢复上一次网络配置？')
        result = getattr(network, args.action)(args.transaction)
    else:
        normal_system(Path('/'))
        if args.action == 'configure':
            require(not args.json, 'interactive_required', 'Use set for JSON/noninteractive configuration')
            emit(network.status(args.interface))
            value = wizard(network, args.interface)
        else:
            value = settings(network.choose_interface(args.interface), args.dhcp, args.address, args.gateway, args.dns)
        result = network.plan(value, args.rollback_after)
        if not args.plan:
            if not args.json:
                emit(result)
            approve(args.yes, '立即应用配置？连接可能断开；须在限时内重新连接并执行 confirm，否则自动回滚。')
            result = network.submit(value, args.rollback_after)
    emit(result, args.json)
    return 0


if __name__ == '__main__':
    raise SystemExit(entry(main))
