# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Create and verify the compressed companion required for every IMG delivery."""
from datetime import datetime, timezone
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import subprocess

from .config import require, sha256


def _atomic_text(path, value):
    temporary = path.with_name(path.name + '.building')
    with temporary.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def planned_image_size(payload_bytes, *, boot_offset_bytes, boot_size_bytes, minimum_bytes=2560 * 1024**2):
    """Budget ext4 metadata and free space without filling the physical eMMC."""
    require(all(isinstance(value, int) and value >= 0 for value in (payload_bytes, boot_offset_bytes, boot_size_bytes, minimum_bytes)), "镜像容量参数无效。")
    margin = max(512 * 1024**2, (payload_bytes + 3) // 4)
    required = boot_offset_bytes + boot_size_bytes + payload_bytes + margin + 128 * 1024**2 + 1024**2
    alignment = 256 * 1024**2
    return ((max(minimum_bytes, required) + alignment - 1) // alignment) * alignment


def package_image(image, *, expected_sha256=None, threads=2):
    """Keep the raw IMG and publish an XZ companion only after a full round trip."""
    image = Path(image).absolute()
    require(image.suffix == '.img' and image.is_file() and not image.is_symlink(),
            '压缩输入必须是已有的普通 .img 文件。')
    require(not any(char in image.name for char in '\r\n\\'), '镜像名称不适合 SHA256SUMS。')
    require(isinstance(threads, int) and 1 <= threads <= 32, '压缩线程数必须为 1 至 32。')
    compressor = shutil.which('xz')
    require(compressor is not None, '缺少 xz；请在构建环境安装 xz-utils。')
    destination = image.with_name(image.name + '.xz')
    temporary = destination.with_name(destination.name + '.building')
    report_path = image.parent / 'compression-verification.json'
    sums_path = image.parent / 'SHA256SUMS'
    for path in (destination, temporary, report_path):
        require(not path.exists() and not path.is_symlink(), '压缩输出已存在：' + str(path))
    require(not sums_path.is_symlink(), 'SHA256SUMS 不能是符号链接。')
    for path in (report_path, sums_path):
        pending = path.with_name(path.name + '.building')
        require(not pending.exists() and not pending.is_symlink(), '存在未完成的交付文件：' + str(pending))
    entries = {}
    if sums_path.exists():
        for line in sums_path.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue
            digest, separator, name = line.partition('  ')
            require(separator and re.fullmatch('[0-9a-f]{64}', digest) and name
                    and name not in entries, '现有 SHA256SUMS 格式无效或文件名重复。')
            entries[name] = digest
    identity = _identity(image)
    print('Calculating raw IMG SHA256 before compression...', flush=True)
    raw_hash = sha256(image)
    require(_identity(image) == identity, '计算校验时原始 IMG 发生变化。')
    require(expected_sha256 is None or raw_hash == expected_sha256, '原始 IMG 的 SHA256 与构建记录不一致。')
    require(entries.get(image.name, raw_hash) == raw_hash, 'SHA256SUMS 中的原始 IMG 哈希不一致。')
    require(destination.name not in entries, 'SHA256SUMS 已包含待生成的压缩文件。')
    environment = os.environ.copy()
    environment.pop('XZ_DEFAULTS', None)
    environment.pop('XZ_OPT', None)
    print(f'Compressing {image.name} with xz -6 -T{threads}...', flush=True)
    created = False
    try:
        with temporary.open('xb') as output:
            created = True
            subprocess.run([compressor, '-6', f'-T{threads}', '--stdout', '--', str(image)],
                           stdout=output, check=True, env=environment)
            output.flush()
            os.fsync(output.fileno())
        print('Verifying the complete decompressed IMG size and SHA256...', flush=True)
        decoded_hash = hashlib.sha256()
        decoded_size = 0
        with lzma.open(temporary, 'rb') as stream:
            for block in iter(lambda: stream.read(4 * 1024**2), b''):
                decoded_hash.update(block)
                decoded_size += len(block)
        require(decoded_size == identity[2] and decoded_hash.hexdigest() == raw_hash,
                '压缩包完整解压后的大小或 SHA256 与原始 IMG 不一致。')
        require(_identity(image) == identity, '压缩与验证期间原始 IMG 发生变化。')
        compressed = {'path': str(destination), 'size_bytes': temporary.stat().st_size,
                      'sha256': sha256(temporary)}
        report = {'schema_version': 1, 'passed': True, 'format': 'xz', 'preset': 6,
                  'threads': threads, 'verified_at': datetime.now(timezone.utc).isoformat(),
                  'image': {'path': str(image), 'size_bytes': identity[2], 'sha256': raw_hash},
                  'compressed_image': compressed,
                  'decoded_image': {'size_bytes': decoded_size, 'sha256': decoded_hash.hexdigest()}}
        temporary.rename(destination)
        _atomic_text(report_path, json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        entries[image.name] = raw_hash
        entries[destination.name] = compressed['sha256']
        _atomic_text(sums_path, ''.join(f'{digest}  {name}\n' for name, digest in sorted(entries.items())))
        print('IMG, IMG.XZ and SHA256SUMS are ready.', flush=True)
        return report
    finally:
        if created and temporary.exists():
            temporary.unlink()
