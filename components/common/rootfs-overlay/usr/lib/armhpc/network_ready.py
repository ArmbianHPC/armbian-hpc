# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Bounded readiness of the selected management path, independent of DHCPv6 state."""
import argparse
import ipaddress
import json
from pathlib import Path
import sys
import time
import subprocess
import urllib.parse
from config_io import emit, entry, read_json, require, run, save_json

DEFAULT = {'interface': 'eth0', 'family': 'ipv4', 'management_target': None}


def policy(root=Path('/'),deadline=None):
    record = read_json(Path(root) / 'var/lib/armhpc/network-config/transaction.json', {})
    selected=None
    if record.get('stage') in ('applying', 'pending', 'confirmed'):
        value = record.get('proposed', {})
        if value.get('policy', {}).get('management'):
            selected={'interface': value['interface'], **value['policy']}
    selected=selected or {**DEFAULT, **read_json(Path(root) / 'etc/armhpc/management-network.json', {})}
    if not selected.get('management_target'):
        settings=read_json(Path(root)/'etc/image-framework/k3s.json',{})
        state_file=settings.get('state_file')
        if state_file and str(state_file).startswith('/data/'):
            state=read_json(Path(root)/str(state_file).lstrip('/'),{})
            host=urllib.parse.urlsplit(state.get('plan',{}).get('server','')).hostname
            if host:
                try:selected['management_target']=str(ipaddress.ip_address(host))
                except ValueError:
                    try:
                        timeout=2 if deadline is None else max(.05,min(2,deadline-time.monotonic()))
                        resolved=run('getent','ahostsv4',host,timeout=timeout).splitlines()
                        if resolved:selected['management_target']=str(ipaddress.ip_address(resolved[0].split()[0]))
                    except (RuntimeError,OSError,subprocess.SubprocessError):pass
    return selected


def inspect(value, root=Path('/'), deadline=None):
    interface = value['interface']
    require(interface and '/' not in interface and interface != 'lo', 'management_interface', 'Invalid management interface')
    family = value.get('family', 'ipv4')
    require(family in ('ipv4', 'ipv6', 'both'), 'management_family', 'Select ipv4, ipv6 or both')
    carrier = Path(root) / 'sys/class/net' / interface / 'carrier'
    errors, addresses, routes = [], {}, {}
    if not carrier.is_file() or carrier.read_text().strip() != '1':
        errors.append('carrier_unavailable')
    for version in ([4, 6] if family == 'both' else [4 if family == 'ipv4' else 6]):
        def query(*args):
            return json.loads(run(*args,timeout=2 if deadline is None else max(.05,min(2,deadline-time.monotonic()))))
        if deadline is not None and time.monotonic()>=deadline:
            errors.append('management_timeout');break
        rows = query('ip', '-j', '-' + str(version), 'address', 'show', 'dev', interface)
        usable = [a['local'] for row in rows for a in row.get('addr_info', []) if a.get('scope') == 'global'
                  and not any(flag in a.get('flags', []) for flag in ('tentative', 'dadfailed','deprecated'))
                  and not (ipaddress.ip_address(a['local']).is_link_local or ipaddress.ip_address(a['local']).is_multicast
                           or ipaddress.ip_address(a['local']).is_unspecified)]
        addresses[str(version)] = usable
        if not usable:
            errors.append('ipv' + str(version) + '_address_missing')
            continue
        target = value.get('management_target')
        if target:
            target_ip = ipaddress.ip_address(target)
            if target_ip.version != version:
                target = None
        if target:
            found = query('ip', '-j', '-' + str(version), 'route', 'get', target)
            valid = [r for r in found if r.get('dev') == interface and r.get('prefsrc', r.get('src')) in usable
                     and r.get('type', 'unicast') == 'unicast']
        else:
            found = query('ip', '-j', '-' + str(version), 'route', 'show', 'default', 'dev', interface)
            valid = found
        routes[str(version)] = valid
        if not valid:
            errors.append('ipv' + str(version) + '_management_route_missing')
    return {'ready': not errors, 'policy': value, 'addresses': addresses, 'routes': routes, 'errors': errors}


def wait(seconds=45):
    deadline = time.monotonic() + min(max(seconds, 0), 45)
    while True:
        try:
            result = inspect(policy(deadline=deadline),deadline=deadline)
        except (RuntimeError, OSError, ValueError,subprocess.SubprocessError) as error:
            result = {'ready': False, 'errors': [str(error)]}
        save_json(Path('/run/armhpc/management-network.json'), result)
        if result['ready'] or time.monotonic() >= deadline:
            return result
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=int, default=45)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    result = wait(args.timeout)
    emit(result, args.json)
    return 0 if result['ready'] else 2


if __name__ == '__main__':
    raise SystemExit(entry(main))
