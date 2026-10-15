# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Bounded local host-health reports; reset causes remain unknown without evidence."""
import argparse
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time
from config_io import atomic_write, emit, entry, read_json, save_json


def read(path):
    try:return Path(path).read_text().strip()
    except OSError:return None


def status():
    result=read_json(Path('/run/armhpc/health.json'),{})
    result['age_seconds']=max(0,time.monotonic()-result.get('monotonic_seconds',0))
    result['stale']=result['age_seconds']>135 or result.get('boot_id')!=read('/proc/sys/kernel/random/boot_id')
    unit=subprocess.run(['systemctl','show','armhpc-health.service','-p','Result','--value'],text=True,capture_output=True,timeout=3)
    result['collector_result']=unit.stdout.strip() if unit.returncode==0 else 'unknown'
    return result


def collect():
    from node_state import status as node_status, kernel_status, data_mount
    from swap_config import status as swap_status
    memory={line.split(':')[0]:line.split(':',1)[1].strip() for line in Path('/proc/meminfo').read_text().splitlines()}
    cpus={p.name:{n:read(p/n) for n in ('related_cpus','scaling_governor','scaling_cur_freq','scaling_max_freq','cpuinfo_max_freq')}
          for p in Path('/sys/devices/system/cpu/cpufreq').glob('policy*')}
    thermal={p.name:{'type':read(p/'type'),'temperature_millicelsius':read(p/'temp')}
             for p in Path('/sys/class/thermal').glob('thermal_zone*')}
    failed=subprocess.run(['systemctl','--failed','--no-legend','--plain','--no-pager'],text=True,capture_output=True,timeout=3)
    report={'schema_version':1,'boot_id':read('/proc/sys/kernel/random/boot_id'),
            'monotonic_seconds':time.monotonic(),'epoch':time.time(),'hostname':platform.node(),
            'kernel':kernel_status(),'memory':memory,'cpufreq':cpus,'thermal':thermal,
            'state':node_status(),'swap':swap_status(),'failed_units':failed.stdout.splitlines(),
            'pstore_files':[p.name for p in Path('/sys/fs/pstore').glob('*') if p.is_file()],
            'reset_cause':'unknown','hardware_verified':False}
    try:
        data,_=data_mount()
        info=os.statvfs(data)
        report['DATA']={'available_bytes':info.f_bavail*info.f_frsize,'available_inodes':info.f_favail}
        folder=data/'node-config/armhpc/health'
        from config_io import safe_path
        folder=safe_path(data,'node-config/armhpc/health')
        previous=sorted(folder.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True) if folder.exists() else []
        report['previous_boot']=next(({'boot_id':p.stem,'last_sample_epoch':read_json(p,{}).get('epoch'),
                                      'reset_cause':'unknown'} for p in previous if p.stem!=report['boot_id'] and not p.is_symlink()),None)
        journal=subprocess.run(['journalctl','--boot','--no-pager','--output=short-monotonic','-n','1500'],
                               text=True,capture_output=True,timeout=3)
        if journal.returncode==0:
            atomic_write(folder/(report['boot_id']+'.log'),journal.stdout.encode()[-512*1024:],0o600)
        report['journal_scope']='Linux journal sampled before reset; DDR/SPL/U-Boot are not captured'
        save_json(folder/(report['boot_id']+'.json'),report)
        records=sorted(folder.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        for previous in records[8:]:
            if not previous.is_symlink():
                previous.unlink()
                log=previous.with_suffix('.log')
                if not log.is_symlink():log.unlink(missing_ok=True)
    except (RuntimeError,OSError,ValueError,subprocess.SubprocessError) as error:
        report['DATA']={'error':str(error)}
    save_json(Path('/run/armhpc/health.json'),report)
    lines=['# HELP armhpc_health_collected_seconds Host collection UTC time.',
           '# TYPE armhpc_health_collected_seconds gauge','armhpc_health_collected_seconds '+str(report['epoch']),
           'armhpc_memory_total_bytes '+str(int(memory['MemTotal'].split()[0])*1024),
           'armhpc_failed_units '+str(len(report['failed_units']))]
    for name,item in thermal.items():
        if item['temperature_millicelsius'] and item['temperature_millicelsius'].lstrip('-').isdigit():
            lines.append('armhpc_temperature_celsius{zone="'+name+'"} '+str(int(item['temperature_millicelsius'])/1000))
    atomic_write(Path('/run/armhpc/metrics/host.prom'),'\n'.join(lines)+'\n',0o644)
    return report


def main():
    parser=argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG','armhpc health'))
    parser.add_argument('action',nargs='?',default='status',choices=('status','collect'))
    parser.add_argument('--json',action='store_true')
    parser.add_argument('--quiet',action='store_true')
    args=parser.parse_args()
    result=collect() if args.action=='collect' else status()
    if not args.quiet:emit(result,args.json)


if __name__=='__main__':raise SystemExit(entry(main))
