#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
import os
from pathlib import Path
import subprocess
import sys

kernel, output = map(Path, sys.argv[1:])
kernel = kernel.resolve(strict=True)
assert output.parent.is_dir() and not output.exists()
result = subprocess.run(['git', '-C', str(kernel), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], capture_output=True, check=True)
paths = sorted(set(result.stdout.split(b'\0')) - {b''})
names = []
for name in paths:
    path = kernel / os.fsdecode(name)
    if path.is_file() or path.is_symlink():
        names.append(name)
assert b'arch/arm64/boot/dts/rockchip/rk3566-wxy-oec-turbo-4g.dts' in names
with output.open('xb') as stream:
    compressor = subprocess.Popen(['zstd', '-T4', '-3'], stdin=subprocess.PIPE, stdout=stream)
    archive = subprocess.Popen(['tar', '-C', str(kernel), '--null', '--verbatim-files-from', '--no-recursion', '-cpf', '-', '-T', '-'],
                               stdin=subprocess.PIPE, stdout=compressor.stdin)
    compressor.stdin.close()
    archive.communicate(b'\0'.join(names) + b'\0')
    assert archive.returncode == 0 and compressor.wait() == 0
print(f'Exported {len(names)} actual patched source files, including OECT DTS.')
