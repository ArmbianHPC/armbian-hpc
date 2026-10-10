# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Validate declared persistent U-Boot environments without exporting opaque values."""
import hashlib
import json
from pathlib import Path
import re
import zlib

from .config import require


BOOT_VARIABLES = {'bootcmd', 'bootcmd_emmc', 'extlinux_env'}


def validate_spec(spec):
    require(spec.get('schema_version') == 1 and spec.get('format') == 'uboot-env-crc32-le',
            'Unsupported persistent U-Boot environment format.')
    for key in ('offset_bytes', 'size_bytes', 'record_size_bytes'):
        require(type(spec.get(key)) is int and spec[key] > 0 and spec[key] % 512 == 0,
                'Environment offset and sizes must be positive, sector-aligned integers.')
    records = spec.get('records')
    require(isinstance(records, list) and records and spec['record_size_bytes'] >= 512
            and spec['size_bytes'] == len(records) * spec['record_size_bytes'],
            'Environment record count or complete size differs.')
    for record in [spec, *records]:
        require(isinstance(record.get('sha256'), str) and re.fullmatch('[0-9a-f]{64}', record['sha256']),
                'Environment SHA256 is required.')
    for record in records:
        variables = record.get('expected_variables')
        require(isinstance(variables, dict) and variables and set(variables) <= BOOT_VARIABLES
                and all(isinstance(value, str) and value and '\0' not in value for value in variables.values()),
                'Only explicit boot variables may be inspected or exported.')
    return spec


def load_oect_environment_spec(project):
    path = Path(project) / 'adapters/boards/wxy-oect-original/boot/environment.json'
    return validate_spec(json.loads(path.read_text(encoding='utf-8')))


def ensure_environment_fits(spec, first_partition_offset):
    validate_spec(spec)
    require(type(first_partition_offset) is int
            and spec['offset_bytes'] + spec['size_bytes'] <= first_partition_offset,
            'OECT environment at 144 MiB overlaps BOOT: legacy 16 MiB BOOT layouts are invalid; use the compact/environment diagnostic builder.')


def inspect_environment(data, spec):
    validate_spec(spec)
    require(len(data) == spec['size_bytes'], 'Persistent environment is incomplete.')
    require(hashlib.sha256(data).hexdigest() == spec['sha256'], 'Persistent environment SHA256 differs.')
    records = []
    for index, expected in enumerate(spec['records']):
        start = index * spec['record_size_bytes']
        record = data[start:start + spec['record_size_bytes']]
        require(hashlib.sha256(record).hexdigest() == expected['sha256'], 'Environment record SHA256 differs.')
        stored = int.from_bytes(record[:4], 'little')
        require(zlib.crc32(record[4:]) == stored, 'Persistent environment CRC32 differs.')
        require(b'\0\0' in record[4:], 'Environment variable list is not terminated.')
        # Keep unselected values opaque, including vendor serial numbers and identifiers.
        selected = {}
        for entry in record[4:].split(b'\0'):
            if not entry:
                break
            key, separator, value = entry.partition(b'=')
            require(separator, 'Environment entry is malformed.')
            for name in expected['expected_variables']:
                if key == name.encode('ascii'):
                    try:
                        selected[name] = value.decode('ascii')
                    except UnicodeDecodeError:
                        require(False, 'Boot variable is not ASCII.')
        require(selected == expected['expected_variables'], 'Persistent environment boot commands differ.')
        records.append({'relative_offset_bytes': start, 'size_bytes': len(record),
                        'sha256': expected['sha256'], 'crc32': f'{stored:08x}',
                        'crc32_valid': True, 'boot_variables': selected})
    return {'passed': True, 'format': spec['format'], 'offset_bytes': spec['offset_bytes'],
            'size_bytes': len(data), 'sha256': spec['sha256'], 'records': records}


def read_environment(image, spec):
    validate_spec(spec)
    with Path(image).open('rb') as stream:
        stream.seek(spec['offset_bytes'])
        data = stream.read(spec['size_bytes'])
    return data, inspect_environment(data, spec)


def environment_component(spec):
    validate_spec(spec)
    return {'id': 'oect-boot-environment', 'role': 'boot-environment',
            'offset_bytes': spec['offset_bytes'], 'size_bytes': spec['size_bytes'],
            'sha256': spec['sha256'], 'provider': 'user-supplied-reference-image',
            'source': spec.get('source', {}), 'redistribution_reviewed': False}


def require_environment_component(components, spec):
    expected = environment_component(spec)
    matches = [item for item in components if item.get('role') == 'boot-environment']
    require(len(matches) == 1 and all(matches[0].get(key) == expected[key]
            for key in ('offset_bytes', 'size_bytes', 'sha256')),
            'OECT boot-environment component is missing or differs; IDB/FIT alone do not define the board boot path.')


def verify_environment_only_change(base, output, spec):
    """Prove that every byte outside the declared environment stayed identical."""
    validate_spec(spec)
    base, output = Path(base), Path(output)
    require(base.stat().st_size == output.stat().st_size, 'Environment repair changed IMG size.')
    start, stop = spec['offset_bytes'], spec['offset_bytes'] + spec['size_bytes']
    require(stop < base.stat().st_size, 'Environment repair is outside IMG.')
    ranges = []
    with base.open('rb') as original, output.open('rb') as repaired:
        for low, high in ((0, start), (stop, base.stat().st_size)):
            original.seek(low)
            repaired.seek(low)
            digest = hashlib.sha256()
            remaining = high - low
            while remaining:
                length = min(4 * 1024**2, remaining)
                before, after = original.read(length), repaired.read(length)
                require(len(before) == length and before == after, 'IMG changed outside the boot environment.')
                digest.update(before)
                remaining -= length
            ranges.append({'start_bytes': low, 'end_bytes_exclusive': high, 'sha256': digest.hexdigest()})
        original.seek(start)
        repaired.seek(start)
        before, after = original.read(spec['size_bytes']), repaired.read(spec['size_bytes'])
    environment = inspect_environment(after, spec)
    changed = [start // 512 + index // 512 for index in range(0, len(after), 512)
               if before[index:index + 512] != after[index:index + 512]]
    return {'passed': True, 'scope': 'full raw IMG', 'outside_environment_identical': True,
            'unchanged_ranges': ranges, 'modified_region': environment,
            'different_sector_count': len(changed), 'different_lbas': changed}
