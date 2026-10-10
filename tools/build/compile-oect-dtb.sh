#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1 LC_ALL=C.UTF-8
if test "$#" -ne 3; then
  printf 'Usage: %s PROJECT PATCHED_LINUX_TREE NEW_OUTPUT_DIRECTORY\n' "$0" >&2
  exit 2
fi
python3 - "$@" <<'PY'
import hashlib, json, pathlib, subprocess, sys
project, tree = (pathlib.Path(value).resolve(strict=True) for value in sys.argv[1:3])
output = pathlib.Path(sys.argv[3]).absolute()
assert output.parent.is_dir() and not output.exists() and not output.is_symlink()
head = subprocess.check_output(['git', '-C', str(tree), 'rev-parse', 'HEAD'], text=True).strip()
assert head == 'e2acc2211022246c77740d5df08265cc27eedcc5'
board = json.loads((project / 'adapters/boards/wxy-oect-original.json').read_text())
source = project / board['boot']['dts']
digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
pin = next(item for item in board['inputs'] if item['path'] == board['boot']['dts'])
assert digest(source) == pin['sha256']
output.mkdir()
preprocessed = output / 'preprocessed.dts'
dtb = output / 'rk3566-wxy-oec-turbo-4g.dtb'
with preprocessed.open('wb') as stream:
    subprocess.run(['cpp', '-nostdinc', '-undef', '-D__DTS__', '-x', 'assembler-with-cpp',
                    '-I', str(tree/'include'), '-I', str(tree/'scripts/dtc/include-prefixes'),
                    '-I', str(tree/'arch/arm64/boot/dts/rockchip'), str(source)], stdout=stream, check=True)
with (output/'dtc.log').open('wb') as stream:
    subprocess.run(['dtc', '-@', '-I', 'dts', '-O', 'dtb', '-o', str(dtb), str(preprocessed)], stderr=stream, check=True)
record = {'schema_version': 1, 'source': str(source), 'source_sha256': digest(source),
          'kernel_tree_head': head, 'preprocessed_sha256': digest(preprocessed),
          'dtb': str(dtb), 'dtb_sha256': digest(dtb)}
(output/'dtb-build.json').write_text(json.dumps(record, indent=2)+'\n')
print(json.dumps(record, indent=2))
PY
