# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Manage only the ARMHPC nftables table, with SSH-session confirmation and rollback."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid
from config_io import (approve,atomic_write,configuration_lock,emit,entry,normal_system,
                       read_json,require,require_idle,run,safe_path,save_json)

STATE='var/lib/armhpc/firewall/state.json'
TABLE='armhpc'


def current():
    result=subprocess.run(['nft','list','table','inet',TABLE],text=True,capture_output=True,timeout=5)
    if result.returncode:
        require('No such file' in result.stderr,'firewall_query','Cannot query the ARMHPC nftables table')
        return ''
    return result.stdout


def settings(role,management,cluster,ssh_port=22,nodeport=False):
    require(role in ('worker','server','server-etcd'),'firewall_role','Select a supported K3s role')
    require(1<=ssh_port<=65535,'firewall_port','Invalid SSH port')
    networks=lambda rows:sorted(set(str(ipaddress.ip_network(item,strict=False)) for item in rows))
    management,cluster=networks(management),networks(cluster)
    require(management and cluster,'firewall_networks','Supply trusted management and cluster node CIDRs')
    connection=os.environ.get('SSH_CONNECTION','').split()
    if connection:
        require(any(ipaddress.ip_address(connection[0]) in ipaddress.ip_network(n) for n in management),
                'firewall_ssh','The current SSH client is outside the selected management CIDRs')
        require(int(connection[3])==ssh_port,'firewall_ssh','The configured SSH port differs from this connection')
    return {'role':role,'management_cidrs':management,'cluster_cidrs':cluster,'ssh_port':ssh_port,'nodeport':bool(nodeport)}


def render(value,transaction=None):
    lines=['table inet '+TABLE+' {']
    if transaction:
        require(re.fullmatch(r'[a-f0-9-]{36}',transaction),'firewall_transaction','Invalid transaction identifier')
        lines.append(' comment "armhpc-transaction:'+transaction+'"')
    lines+=[' chain input {','  type filter hook input priority -10; policy drop;',
           '  iifname "lo" accept','  ct state established,related accept',
           '  ct state invalid drop','  meta l4proto { icmp, ipv6-icmp } accept',
           '  udp sport 67 udp dport 68 accept','  udp sport 547 udp dport 546 accept']
    for cidr in value['management_cidrs']:
        prefix='ip6' if ipaddress.ip_network(cidr).version==6 else 'ip'
        lines.append('  '+prefix+' saddr '+cidr+' tcp dport '+str(value['ssh_port'])+' accept')
    for cidr in sorted(set(value['management_cidrs']+value['cluster_cidrs'])):
        prefix='ip6' if ipaddress.ip_network(cidr).version==6 else 'ip'
        lines+=['  '+prefix+' saddr '+cidr+' udp dport { 53, 123 } accept',
                '  '+prefix+' saddr '+cidr+' tcp dport 53 accept']
        if value['role']!='worker':lines.append('  '+prefix+' saddr '+cidr+' tcp dport 6443 accept')
        if value['nodeport']:
            lines+=['  '+prefix+' saddr '+cidr+' tcp dport 30000-32767 accept',
                    '  '+prefix+' saddr '+cidr+' udp dport 30000-32767 accept']
    for cidr in value['cluster_cidrs']:
        prefix='ip6' if ipaddress.ip_network(cidr).version==6 else 'ip'
        lines+=['  '+prefix+' saddr '+cidr+' tcp dport 10250 accept',
                '  '+prefix+' saddr '+cidr+' udp dport 8472 accept']
        if value['role']=='server-etcd':lines.append('  '+prefix+' saddr '+cidr+' tcp dport 2379-2380 accept')
    return '\n'.join([*lines,' }','}',''])


def replace(rules):
    existing=current()
    batch=('delete table inet '+TABLE+'\n' if existing else '')+rules
    for flags in (['--check'],[]):
        result=subprocess.run(['nft',*flags,'-f','-'],input=batch,text=True,capture_output=True,timeout=5)
        require(result.returncode==0,'firewall_rules','nftables rejected the owned-table transaction: '+result.stderr[-400:])


def state():return read_json(safe_path(Path('/'),STATE),{'stage':'disabled','schema_version':1})
def save(value):save_json(safe_path(Path('/'),STATE),value)
def checksum(text):return hashlib.sha256(text.encode()).hexdigest()


def rollback(value):
    observed=current()
    if not value.get('applied_hash') and observed==value['backup']:
        save(value['previous']);return
    require(checksum(observed)==value.get('applied_hash') or
            not value.get('applied_hash') and 'comment "armhpc-transaction:'+value['transaction_id']+'"' in observed,
            'firewall_conflict','Owned firewall rules changed externally; inspect before overwriting')
    replace(value['backup'])
    previous=value.get('previous',{'stage':'disabled','schema_version':1})
    previous['last_transaction']={'id':value['transaction_id'],'result':'rolled-back'}
    save(previous)


def apply(value):
    record=state();require(record['stage']!='pending','firewall_pending','Confirm or roll back the pending firewall change')
    old=current()
    require(not old or checksum(old)==record.get('applied_hash'),'firewall_conflict','An unmanaged or modified ARMHPC table already exists')
    record={'schema_version':1,'stage':'pending','transaction_id':str(uuid.uuid4()),'policy':value,
            'backup':old,'previous':record,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'deadline':time.clock_gettime(time.CLOCK_BOOTTIME)+90,'initiator':os.environ.get('SSH_CONNECTION','')}
    save(record)
    try:
        replace(render(value,record['transaction_id']))
        record['applied_hash']=checksum(current());save(record)
        run('systemd-run','--quiet','--unit=armhpc-firewall-'+record['transaction_id'],
            '--property=RuntimeMaxSec=120','--property=Restart=on-failure',
            '/usr/bin/python3',Path(__file__).resolve(),'_watch','--transaction',record['transaction_id'])
    except Exception:
        replace(old);save(record['previous']);raise
    return public(record)


def public(record):return {k:v for k,v in record.items() if k not in ('backup','previous')}


def confirm(identifier):
    value=state()
    require(value['stage']=='pending' and value['transaction_id']==identifier,'firewall_transaction','No matching pending firewall transaction')
    require(value['boot_id']==Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            and time.clock_gettime(time.CLOCK_BOOTTIME)<value['deadline'],'firewall_expired','Confirmation expired')
    connection=os.environ.get('SSH_CONNECTION','')
    require(connection and connection!=value['initiator'],'firewall_session','Confirm from a separate SSH connection')
    require(checksum(current())==value['applied_hash'],'firewall_conflict','Owned rules changed externally')
    value={k:v for k,v in value.items() if k not in ('backup','previous','deadline','initiator')}
    value['stage']='confirmed';save(value);return public(value)


def recover(watch=False,identifier=None):
    while True:
        with configuration_lock(Path('/')):
            value=state()
            if value['stage']!='pending' or identifier and value.get('transaction_id')!=identifier:return
            if value['boot_id']!=Path('/proc/sys/kernel/random/boot_id').read_text().strip() or time.clock_gettime(time.CLOCK_BOOTTIME)>=value['deadline']:
                rollback(value);return
        if not watch:return
        time.sleep(1)


def boot():
    value=state()
    if value['stage']=='pending':
        replace(value['backup']);save(value.get('previous',{'stage':'disabled','schema_version':1}));value=state()
    if value['stage']=='confirmed':
        existing=current()
        if existing:
            require(checksum(existing)==value.get('applied_hash'),'firewall_conflict','The ARMHPC table was changed outside its managed policy')
            return
        replace(render(value['policy']));value['applied_hash']=checksum(current());save(value)


def main():
    parser=argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG','armhpc firewall'))
    parser.add_argument('action',nargs='?',default='status',choices=('status','set','confirm','rollback','_boot','_watch'))
    parser.add_argument('--role',choices=('worker','server','server-etcd'),default='worker')
    parser.add_argument('--management-cidr',action='append',default=[]);parser.add_argument('--cluster-cidr',action='append',default=[])
    parser.add_argument('--ssh-port',type=int,default=22);parser.add_argument('--nodeport',action='store_true')
    parser.add_argument('--transaction');parser.add_argument('--apply',action='store_true');parser.add_argument('--yes',action='store_true');parser.add_argument('--json',action='store_true')
    args=parser.parse_args();normal_system(Path('/'))
    if args.action=='_watch':recover(True,args.transaction);return
    with configuration_lock(Path('/')):
        if args.action=='_boot':boot();return
        if args.action=='status':result=public(state())
        else:
            require_idle(Path('/'))
            if args.action=='set':
                value=settings(args.role,args.management_cidr,args.cluster_cidr,args.ssh_port,args.nodeport)
                result={'policy':value,'rules':render(value),'external_rules':'preserved; external drop rules may still deny allowed traffic'}
                if args.apply:
                    approve(args.yes,'Apply this host firewall and confirm from a separate SSH connection within 90 seconds?')
                    result=apply(value)
            elif args.action=='confirm':result=confirm(args.transaction)
            else:
                value=state();require(value.get('transaction_id')==args.transaction and value['stage']=='pending','firewall_transaction','Use the original pending transaction')
                approve(args.yes,'Roll back this firewall transaction?');rollback(value);result=public(state())
    emit(result,args.json)


if __name__=='__main__':raise SystemExit(entry(main))
