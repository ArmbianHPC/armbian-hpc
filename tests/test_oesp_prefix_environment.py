# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util
from pathlib import Path
import struct
import os
import shutil
import subprocess
import zlib
import pytest

script = Path(__file__).parents[1] / 'tools/build/prepare-oesp-prefix.py'
spec = importlib.util.spec_from_file_location('oesp_prefix_environment', script)
prefix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prefix)
from framework.amlogic_boot import vendor_environment, validate_vendor_environment, decode_environment


def environment(redundant=False):
    data = b'bootcmd=run armhpc_mmc_boot\0\0'
    data += b'\xff' * (65536 - (5 if redundant else 4) - len(data))
    return struct.pack('<I', zlib.crc32(data)) + (b'\x01' if redundant else b'') + data


def test_vendor_nonredundant_environment_is_accepted():
    prefix.validate_environment(environment())


def test_redundancy_flag_format_is_refused():
    with pytest.raises(ValueError, match='4-byte CRC header'):
        prefix.validate_environment(environment(True))


def test_corrupt_or_truncated_environment_is_refused():
    data = bytearray(environment()); data[50] ^= 1
    for value in (bytes(data), environment()[:-1]):
        with pytest.raises(ValueError, match='4-byte CRC header'):
            prefix.validate_environment(value)


def test_board_phy_policy_includes_supported_realtek_id():
    import json
    p = Path(__file__).parents[1] / 'adapters/boards/wxy-oesp-v22/rootfs-overlay/etc/armhpc/phy-led.json'
    assert int(json.loads(p.read_text())['phy_id'], 16) == 0x001cc916


def encode_values(values):
    payload = b'\0'.join((key + '=' + value).encode('ascii') for key, value in values.items()) + b'\0\0'
    payload += b'\xff' * (65536 - 4 - len(payload))
    return struct.pack('<I', zlib.crc32(payload)) + payload


def test_generated_entry_executes_vendor_autoscr_and_has_no_device_identity():
    values = vendor_environment()
    block = encode_values(values)
    assert decode_environment(block) == values
    assert validate_vendor_environment(block)['script_command'] == 'autoscr'
    assert not {'ethaddr', 'serial', 'serial#', 'mac', 'ipaddr', 'preboot', 'storeboot'} & values.keys()


@pytest.mark.parametrize('fault', ['source', 'wrong_mmc', 'wrong_address', 'identity', 'android_preboot'])
def test_valid_crc_does_not_make_incompatible_entry_acceptable(fault):
    values = vendor_environment()
    if fault == 'source':
        values['try_emmc_bootcmd'] = values['try_emmc_bootcmd'].replace('autoscr', 'source')
    elif fault == 'wrong_mmc':
        values['try_emmc_bootcmd'] = values['try_emmc_bootcmd'].replace('fatload mmc 1', 'fatload mmc 0')
    elif fault == 'wrong_address':
        values['try_emmc_bootcmd'] = values['try_emmc_bootcmd'].replace('1020000', '10000000')
    elif fault == 'identity':
        values['ethaddr'] = '02:00:00:00:00:01'
    else:
        values['preboot'] = 'run storeargs; forceupdate; run switch_bootmode'
    block = encode_values(values)
    assert decode_environment(block) == values
    with pytest.raises(ValueError, match='vendor boot entry differs'):
        validate_vendor_environment(block)


def test_repeated_environment_keys_are_refused_even_with_valid_crc():
    payload = b'bootcmd=run autobootcmd\0bootcmd=run storeboot\0\0'
    payload += b'\xff' * (65536 - 4 - len(payload))
    with pytest.raises(ValueError, match='duplicate'):
        decode_environment(struct.pack('<I', zlib.crc32(payload)) + payload)


@pytest.mark.skipif(not shutil.which('mkenvimage'), reason='U-Boot host tool required')
def test_real_mkenvimage_output_passes_entry_validation(tmp_path):
    source, binary = tmp_path / 'env.txt', tmp_path / 'env.bin'
    source.write_text(prefix.environment_text(), encoding='ascii')
    subprocess.run(['mkenvimage', '-s', '65536', '-o', str(binary), str(source)], check=True)
    assert validate_vendor_environment(binary.read_bytes())['script_address'] == '0x1020000'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX shell required for Hush branch simulation')
@pytest.mark.parametrize('target,usb_ready,expected', [
    ('mmc:1', 'yes', ['mmc 1']),
    ('usb:2', 'yes', ['mmc 1', 'usb 0', 'usb 1', 'usb 2']),
    ('missing', 'yes', ['mmc 1', 'usb 0', 'usb 1', 'usb 2', 'usb 3']),
    ('missing', 'no', ['mmc 1']),
])
def test_entry_branches_preserve_selected_medium(target, usb_ready, expected):
    # Run only the generated Hush-compatible control flow with inert I/O stubs.
    script = '''
setenv() { export "$1=$2"; }
run() { eval "$(printenv "$1")"; }
fatload() { printf 'load %s %s %s %s\\n' "$1" "$2" "$3" "$4"; test "$1:$2" = "$BOOT_TARGET"; }
usb() { test "$USB_READY" = yes; }
autoscr() { printf 'execute %s %s %s\\n' "$devtype" "$devnum" "$1"; exit 0; }
run bootcmd
exit 9
'''
    result = subprocess.run(['/bin/sh', '-c', script], env={**os.environ, **vendor_environment(),
                            'BOOT_TARGET': target, 'USB_READY': usb_ready}, capture_output=True, text=True)
    loads = [line for line in result.stdout.splitlines() if line.startswith('load ')]
    assert loads == ['load ' + device + ' 1020000 boot.scr' for device in expected]
    if target == 'missing':
        assert result.returncode == 9 and 'execute ' not in result.stdout
    else:
        assert result.returncode == 0
        assert 'execute ' + target.replace(':', ' ') + ' 1020000' in result.stdout
