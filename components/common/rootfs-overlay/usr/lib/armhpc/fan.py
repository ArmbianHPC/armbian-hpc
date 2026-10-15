#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Board-configured GPIO fan modes with thermal protection and safe fallback."""
import argparse, json, os, signal, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_io import save_json, configuration_lock, require, approve, emit, entry

POLICY = 'etc/armhpc/fan.json'
SETTINGS = 'var/lib/armhpc/fan-settings.json'

class Fan:
    def __init__(self, root=Path('/')):
        self.root = Path(root)

    def policy(self):
        p = self.root/POLICY
        if not p.is_file(): return {'enabled':False}
        value = json.loads(p.read_text())
        require(value.get('cooling_type') == 'gpio-fan' and value.get('on_millicelsius') == 55000
                and value.get('off_millicelsius') == 47000, 'fan_policy', 'Unknown fan policy')
        return value

    def mode(self):
        p = self.root/SETTINGS
        value = json.loads(p.read_text()) if p.is_file() else {'mode':'auto'}
        require(value.get('mode') in ('auto','on','off'), 'fan_state', 'Invalid stored fan mode')
        return value['mode']

    def hardware(self):
        matches = []
        for p in (self.root/'sys/class/hwmon').glob('hwmon*'):
            try:
                if (p/'name').read_text().strip() == 'gpio_fan' and (p/'pwm1').is_file(): matches.append(p)
            except OSError: pass
        require(len(matches) == 1, 'fan_hardware', 'A unique gpio-fan hwmon device is required')
        return matches[0]

    def temperature(self):
        matches = []
        for p in (self.root/'sys/class/thermal').glob('thermal_zone*'):
            try:
                if (p/'type').read_text().strip().replace('_','-') in ('cpu-thermal','soc-thermal'):
                    value = int((p/'temp').read_text())
                    if -20000 <= value <= 150000: matches.append(value)
            except (OSError,ValueError): pass
        return max(matches) if matches else None

    def status(self):
        policy = self.policy()
        if not policy.get('enabled'): return {'supported':False,'reason':'board_has_no_fan_adapter'}
        hw = self.hardware(); temp = self.temperature()
        return {'supported':True,'mode':self.mode(),'temperature_millicelsius':temp,
                'enabled':int((hw/'pwm1').read_text()) > 0,'kind':'on-off',
                'temperature_valid':temp is not None,'on_millicelsius':55000,'off_millicelsius':47000,
                'rpm_feedback':False}

    def set(self, mode):
        require(os.geteuid()==0 if self.root==Path('/') else True, 'root_required', 'Run fan settings as root')
        require(self.policy().get('enabled'), 'fan_unsupported', 'This board has no fan adapter')
        require(mode in ('auto','on','off'), 'fan_mode', 'Use auto, on or off')
        self.hardware()
        with configuration_lock(self.root): save_json(self.root/SETTINGS, {'protocol':1,'mode':mode})
        self.tick(self.status()['enabled'])
        return self.status()

    def safe_on(self):
        hw=self.hardware(); (hw/'pwm1_enable').write_text('0\n')

    def tick(self, protected):
        hw=self.hardware(); mode=self.mode(); temp=self.temperature()
        protected = temp is None or temp >= 55000 or (protected and temp > 47000)
        if mode=='on' or protected:
            (hw/'pwm1_enable').write_text('0\n')
        elif mode=='auto':
            (hw/'pwm1_enable').write_text('1\n')
        else:
            (hw/'pwm1_enable').write_text('1\n'); (hw/'pwm1').write_text('0\n')
        return protected

def main(argv=None):
    arguments=list(sys.argv[1:] if argv is None else argv)
    f=Fan()
    if arguments==['_safe-on']: f.safe_on();return
    if arguments==['_service']:
        signal.signal(signal.SIGTERM, lambda *_:sys.exit(0))
        protected=True
        try:
            while True:
                try: protected=f.tick(protected)
                except Exception:
                    f.safe_on(); protected=True
                time.sleep(1)
        finally: f.safe_on()
    p=argparse.ArgumentParser(prog=globals().get('ARMHPC_PROG'),description=__doc__)
    p.add_argument('action',nargs='?',default='status',choices=('status','set'))
    p.add_argument('--mode',choices=('auto','on','off'));p.add_argument('--yes',action='store_true');p.add_argument('--json',action='store_true')
    a=p.parse_args(arguments)
    if a.action=='set':
        require(a.mode is not None,'fan_mode','Provide --mode')
        approve(a.yes,'设置风扇模式 '+a.mode+'？高温或温度读取失败时将强制开启。')
        emit(f.set(a.mode),a.json)
    else: emit(f.status(),a.json)

if __name__=='__main__': entry(main)
