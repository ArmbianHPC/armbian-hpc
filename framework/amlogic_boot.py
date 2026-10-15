# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""OESP vendor U-Boot entry and envelopes; OTA initramfs files remain raw."""
from pathlib import Path
import struct
import zlib

HEADER = struct.Struct('>7I4B32s')
RAMDISK_MAGIC = 0x27051956
KERNEL_LOAD = 0x08080000
FDT_LOAD = 0x08008000
RAMDISK_LOAD = 0x13000000
FDT_HIGH = 0x20000000
SCRIPT_LOAD = 0x01020000
ENVIRONMENT_BYTES = 65536
BOOT_ENTRY_PROFILE = 'oesp-autoscr-v1'


def vendor_environment():
    """Generate the OESP autoscr entry without importing another device's identity."""
    # Command and MMC/address conventions follow the OESP vendor autoscript:
    # https://github.com/retro98boy/amlogic-fip-blobs/blob/main/onethingcloud-oes-plus/aml_autoscript.cmd
    address = format(SCRIPT_LOAD, 'x')
    return {
        'bootdelay': '2', 'upgrade_step': '2', 'reboot_mode': 'normal',
        'armhpc_boot_entry': BOOT_ENTRY_PROFILE,
        'bootcmd': 'run autobootcmd',
        'autobootcmd': ('run try_emmc_bootcmd; if usb start; then run try_usbdrive_bootcmd; fi; '
                        'echo ARMHPC boot files not loaded'),
        'try_emmc_bootcmd': (f'if fatload mmc 1 {address} boot.scr; then '
                             f'setenv devtype mmc; setenv devnum 1; autoscr {address}; fi'),
        'try_usbdrive_bootcmd': ('for usbdevnum in 0 1 2 3; do '
                                 f'if fatload usb ${{usbdevnum}} {address} boot.scr; then '
                                 f'setenv devtype usb; setenv devnum ${{usbdevnum}}; autoscr {address}; '
                                 'fi; done'),
        'kernel_addr_r': hex(KERNEL_LOAD), 'fdt_addr_r': hex(FDT_LOAD),
        'ramdisk_addr_r': hex(RAMDISK_LOAD), 'loadaddr': hex(SCRIPT_LOAD),
        'fdt_high': hex(FDT_HIGH), 'stdin': 'serial', 'stdout': 'serial',
        'stderr': 'serial', 'baudrate': '115200',
    }


def environment_text():
    return ''.join(key + '=' + value + '\n' for key, value in vendor_environment().items())


def decode_environment(block):
    if (len(block) != ENVIRONMENT_BYTES or
            struct.unpack_from('<I', block)[0] != zlib.crc32(block[4:])):
        raise ValueError('OESP vendor environment needs a 4-byte CRC header')
    payload, terminator, padding = block[4:].partition(b'\0\0')
    if not terminator or not payload:
        raise ValueError('Vendor environment is missing its double-NUL terminator')
    values = {}
    for record in payload.split(b'\0'):
        try:
            key, separator, value = record.decode('ascii').partition('=')
        except UnicodeDecodeError as error:
            raise ValueError('Vendor environment must contain ASCII entries') from error
        if not separator or not key or key in values or any(c.isspace() for c in key):
            raise ValueError('Malformed or duplicate vendor environment entry')
        values[key] = value
    return values


def validate_vendor_environment(block):
    values = decode_environment(block)
    expected = vendor_environment()
    if values != expected:
        keys = sorted(key for key in set(values) | set(expected) if values.get(key) != expected.get(key))
        raise ValueError('OESP vendor boot entry differs from ' + BOOT_ENTRY_PROFILE + ': ' + ', '.join(keys))
    return {'profile': BOOT_ENTRY_PROFILE, 'script_command': 'autoscr',
            'script_address': hex(SCRIPT_LOAD), 'emmc_device': 1, 'usb_devices': [0, 1, 2, 3]}


def validate_ramdisk(data):
    if len(data) < HEADER.size:
        raise ValueError('Truncated vendor ramdisk header')
    magic, hcrc, epoch, size, load, entry, dcrc, os_id, arch, kind, comp, name = HEADER.unpack_from(data)
    head = bytearray(data[:HEADER.size]); struct.pack_into('>I', head, 4, 0)
    if magic != RAMDISK_MAGIC or zlib.crc32(head) != hcrc:
        raise ValueError('Vendor ramdisk header CRC differs')
    payload = data[HEADER.size:]
    if size != len(payload) or zlib.crc32(payload) != dcrc:
        raise ValueError('Vendor ramdisk payload size or CRC differs')
    if (os_id, arch, kind, comp) != (5, 2, 3, 0) or load != 0 or entry != 0:
        raise ValueError('Vendor ramdisk must use Linux/ARM/ramdisk/none')
    if payload[:4] == struct.pack('>I', RAMDISK_MAGIC):
        raise ValueError('Nested vendor ramdisk header')
    return payload


def wrap_ramdisk(raw, output, epoch):
    payload = Path(raw).read_bytes()
    if len(payload) == 0 or len(payload) > 0xffffffff or payload[:4] == struct.pack('>I', RAMDISK_MAGIC):
        raise ValueError('Expected a raw initramfs payload')
    head = HEADER.pack(RAMDISK_MAGIC, 0, int(epoch), len(payload), 0, 0, zlib.crc32(payload),
                       5, 2, 3, 0, b'ARMHPC OESP initramfs')
    crc = zlib.crc32(head)
    data = head[:4] + struct.pack('>I', crc) + head[8:] + payload
    if validate_ramdisk(data) != payload:
        raise ValueError('Vendor envelope changed the raw initramfs')
    Path(output).write_bytes(data)


def validate_boot_memory(kernel, dtb, ramdisk):
    image = Path(kernel).read_bytes()
    tree = Path(dtb).read_bytes()
    legacy = Path(ramdisk).read_bytes(); validate_ramdisk(legacy)
    if len(image) < 64 or image[56:60] != b'ARMd':
        raise ValueError('Expected an ARM64 Image')
    offset, memory_size = struct.unpack_from('<QQ', image, 8)
    if offset != 0x01080000 or memory_size < len(image):
        raise ValueError('Kernel does not match the OESP legacy text-offset ABI')
    if len(tree) < 40 or tree[:4] != b'\xd0\x0d\xfe\xed' or struct.unpack_from('>I', tree, 4)[0] != len(tree):
        raise ValueError('Invalid paired DTB')
    regions = [('kernel-load', KERNEL_LOAD, len(image)), ('kernel-entry', offset, memory_size),
               ('dtb', FDT_LOAD, len(tree) + 65536), ('ramdisk', RAMDISK_LOAD, len(legacy)),
               ('script', SCRIPT_LOAD, 65536)]
    for i, (name, start, size) in enumerate(regions):
        if start + size >= FDT_HIGH:
            raise ValueError('Boot object exceeds the vendor low-memory window: ' + name)
        for other, left, count in regions[:i]:
            if start < left + count and left < start + size:
                raise ValueError('Boot memory overlaps: ' + name + '/' + other)
    return {name: {'address': hex(start), 'bytes': size} for name, start, size in regions}
