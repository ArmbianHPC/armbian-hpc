#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise pinned Armbian's real extension lookup/dispatch, with I/O stubs."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from framework.config import require, resolve, sha256
from framework.staging import kernel_extension, rkbin_extension, write_text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('target', nargs='?', default='wxy-oect-k3s')
    parser.add_argument('--archive', type=Path, help='可选：同一 SHA256 的现有 Armbian v25.08 tar。')
    args = parser.parse_args()
    resolved = resolve(ROOT, args.target, require_sources=False)
    item = next(item for item in resolved['inputs'] if item['id'] == 'build-framework')
    archive = args.archive or ROOT / item['path']
    require(sha256(archive) == item['sha256'], 'Armbian 测试快照校验失败。')
    bash = Path(r'C:\Program Files\Git\bin\bash.exe')
    executable = str(bash) if bash.exists() else shutil.which('bash')
    require(executable is not None, '需要 Bash。')
    tests = ROOT / 'build/tests'
    tests.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='backend-hooks-', dir=tests) as temporary:
        folder = Path(temporary).resolve()
        require(folder.is_relative_to(tests.resolve()), '测试目录越界。')
        with tarfile.open(archive) as tar:
            for relative in ('lib/functions/general/extensions.sh', 'extensions/rkbin-tools.sh', 'lib/functions/main/build-packages.sh'):
                path = folder / 'upstream' / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(tar.extractfile('armbian-build-v25.08/' + relative).read())
        write_text(folder / 'userpatches/extensions/image-framework.sh', kernel_extension(resolved))
        write_text(folder / 'userpatches/extensions/rkbin-tools.sh', rkbin_extension())
        family = resolved['soc']['backend_families'][resolved['backend']['id']]
        harness = f'''#!/bin/bash
set -e
export PATH="/usr/bin:/bin:$PATH"
here="$(cd -- "$(dirname -- "${{BASH_SOURCE[0]}}")" && pwd)"
SRC="$here/upstream"
USERPATCHES_PATH="$here/userpatches"
EXTENSION_MANAGER_TMP_DIR="$here/extensions-tmp"
SHOW_DEBUG=no SHOW_EXTENSIONS=no WRITE_EXTENSIONS_METADATA=no
function display_alert() {{ :; }}
function get_extension_hook_stracktrace() {{ :; }}
function add_cleanup_handler() {{ :; }}
function exit_with_error() {{ echo "$*" >&2; exit 87; }}
function fetch_from_repo() {{ exit 88; }}
function install() {{ exit 89; }}
source "$SRC/lib/functions/general/extensions.sh"
extension_manager_declare_globals
[[ "${{1:-}}" == upstream ]] && USERPATCHES_PATH="$here/empty"
enable_extension rkbin-tools
if [[ "${{1:-}}" == upstream ]]; then
    fetch_sources_tools__rkbin_tools
    exit 90
fi
enable_extension image-framework
initialize_extension_manager
BOARDFAMILY={family} BRANCH=current BOOTCONFIG=vendor BOOTCONFIG_CURRENT=vendor
KERNELBRANCH=branch:rolling ATF_COMPILE=yes ATFSOURCE=vendor INSTALL_ARMBIAN_FIRMWARE=yes
post_family_config
test -z "${{BOOTCONFIG_CURRENT:-}}"
# Mimic late lib.config/user_config overrides, then call the pinned final config hook.
BOOTCONFIG=vendor ATF_COMPILE=yes ATFSOURCE=vendor INSTALL_ARMBIAN_FIRMWARE=yes KERNELBRANCH=branch:rolling
extension_prepare_config
test "$KERNELBRANCH" = commit:{resolved['soc']['kernel']['commit']}
test "$BOOTCONFIG" = none
test "$ATF_COMPILE" = no
test "$INSTALL_ARMBIAN_FIRMWARE" = no
fetch_sources_tools
build_host_tools
source "$SRC/lib/functions/main/build-packages.sh"
DISTRIBUTION=Debian RELEASE=bookworm BUILD_MINIMAL=yes PLYMOUTH=no
declare -a artifacts_to_build=()
determine_artifacts_to_build_for_image
for artifact in "${{artifacts_to_build[@]}}"; do
    [[ "$artifact" != uboot && "$artifact" != firmware && "$artifact" != full_firmware ]]
done
printf '%s\\n' "${{artifacts_to_build[*]}}"
'''
        write_text(folder / 'verify.sh', harness)
        result = subprocess.run([executable, '--noprofile', '--norc', (folder / 'verify.sh').as_posix()], capture_output=True, text=True)
        require(result.returncode == 0, '后端钩子验证失败：' + result.stderr)
        positive = subprocess.run([executable, '--noprofile', '--norc', (folder / 'verify.sh').as_posix(), 'upstream'], capture_output=True, text=True)
        require(positive.returncode == 88, '测试未捕获原始 rkbin 自动获取。')
        print(json.dumps({'passed': True, 'pinned_backend_sha256': item['sha256'],
                          'actual_extension_manager': True, 'upstream_fetch_guard_triggered': True,
                          'rkbin_fetch_or_install_called': False, 'selected_artifacts': result.stdout.strip().split(),
                          'kernel_and_boot_policies_verified': True, 'hardware_verified': False}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
