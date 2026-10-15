# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util
import struct
from pathlib import Path
import pytest

script = Path(__file__).parents[1] / 'framework/amlogic_boot.py'
spec = importlib.util.spec_from_file_location('amlogic_boot', script)
boot = importlib.util.module_from_spec(spec); spec.loader.exec_module(boot)


def test_vendor_envelope_keeps_raw_signed_payload(tmp_path):
    raw = bytes.fromhex('28b52ffd') + b'ARMHPC initramfs payload\n'
    source = tmp_path/'initrd'; target = tmp_path/'uInitrd'
    source.write_bytes(raw); boot.wrap_ramdisk(source, target, 1700000000)
    data = target.read_bytes()
    assert boot.validate_ramdisk(data) == raw
    assert data[:4] == bytes.fromhex('27051956')
    assert data[28:32] == bytes((5, 2, 3, 0))
    assert struct.unpack_from('>I', data, 12)[0] == len(raw)


def test_corruption_and_nested_headers_are_rejected(tmp_path):
    source = tmp_path/'initrd'; target = tmp_path/'uInitrd'
    source.write_bytes(b'070701' + b'x'*1024); boot.wrap_ramdisk(source, target, 1700000000)
    for pos in (12, 70):
        broken = bytearray(target.read_bytes()); broken[pos] ^= 1
        with pytest.raises(ValueError, match='CRC|size'):
            boot.validate_ramdisk(broken)
    with pytest.raises(ValueError, match='raw initramfs'):
        boot.wrap_ramdisk(target, tmp_path/'double', 1700000000)


def test_paired_image_ranges_and_legacy_offset_are_required(tmp_path):
    kernel = tmp_path/'Image'; dtb = tmp_path/'dtb'; ramdisk = tmp_path/'uInitrd'; raw = tmp_path/'raw'
    data = bytearray(4096); data[56:60] = b'ARMd'; struct.pack_into('<QQ', data, 8, 0x1080000, 0x3000000)
    kernel.write_bytes(data); dtb.write_bytes(bytes.fromhex('d00dfeed') + struct.pack('>I', 40) + bytes(32))
    raw.write_bytes(b'070701' + b'x'*128); boot.wrap_ramdisk(raw, ramdisk, 1700000000)
    regions = boot.validate_boot_memory(kernel, dtb, ramdisk)
    assert 'kernel-entry' in regions
    assert regions['script']['address'] == '0x1020000'
    assert int(regions['script']['address'], 16) + regions['script']['bytes'] < 0x1080000
    struct.pack_into('<Q', data, 8, 0); kernel.write_bytes(data)
    with pytest.raises(ValueError, match='text-offset ABI'):
        boot.validate_boot_memory(kernel, dtb, ramdisk)
