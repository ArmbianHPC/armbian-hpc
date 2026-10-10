# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Thin bridge to the separately released single OTA engine."""
import os
from pathlib import Path
import sys

sys.path.insert(0, '/usr/lib/armhpc-ota')
os.environ['ARMHPC_OTA_PROG'] = globals().get('ARMHPC_PROG', 'armhpc ota')
try:
    from armhpc_ota.cli import main
except ImportError:
    print('OTA runtime 尚未安装。请安装 armhpc-ota 发布包；源码位于独立仓库。', file=sys.stderr)
    raise SystemExit(2)
if __name__ == '__main__':
    raise SystemExit(main())
