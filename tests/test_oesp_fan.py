# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util,json,sys
from pathlib import Path
import pytest

LIB=Path(__file__).parents[1]/'components/common/rootfs-overlay/usr/lib/armhpc'
sys.path.insert(0,str(LIB))
spec=importlib.util.spec_from_file_location('armhpc_fan',LIB/'fan.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

def fan(tmp_path,temp=30000):
    policy=tmp_path/'etc/armhpc/fan.json';policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({'enabled':True,'cooling_type':'gpio-fan','on_millicelsius':55000,'off_millicelsius':47000}))
    hw=tmp_path/'sys/class/hwmon/hwmon0';hw.mkdir(parents=True)
    for name,value in [('name','gpio_fan'),('pwm1','0'),('pwm1_enable','1')]: (hw/name).write_text(value)
    sensor=tmp_path/'sys/class/thermal/thermal_zone0';sensor.mkdir(parents=True)
    (sensor/'type').write_text('cpu-thermal');(sensor/'temp').write_text(str(temp))
    return module.Fan(tmp_path),hw,sensor

def test_fan_modes_are_board_scoped(tmp_path):
    assert module.Fan(tmp_path).status()['supported'] is False

def test_temperature_failure_and_overheat_force_on(tmp_path):
    f,hw,sensor=fan(tmp_path,55000)
    assert f.tick(False) is True and (hw/'pwm1_enable').read_text().strip()=='0'
    (sensor/'temp').unlink()
    assert f.tick(False) is True

def test_protection_has_hysteresis(tmp_path):
    f,hw,sensor=fan(tmp_path,50000)
    assert f.tick(True) is True
    (sensor/'temp').write_text('47000')
    assert f.tick(True) is False

def test_off_mode_cannot_override_hot_sensor(tmp_path):
    f,hw,sensor=fan(tmp_path,60000)
    f.set('off')
    assert f.mode()=='off' and (hw/'pwm1_enable').read_text().strip()=='0'

def test_manual_on_and_auto(tmp_path):
    f,hw,sensor=fan(tmp_path)
    f.set('on');assert (hw/'pwm1_enable').read_text().strip()=='0'
    f.set('auto');assert (hw/'pwm1_enable').read_text().strip()=='1'
    assert f.status()['rpm_feedback'] is False
