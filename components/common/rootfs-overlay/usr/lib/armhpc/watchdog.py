# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import os
import sys
sys.path.insert(0, '/usr/lib/armhpc-ota')
from armhpc_ota.watchdog import main
os.environ['ARMHPC_WATCHDOG_PROG'] = globals().get('ARMHPC_PROG', 'armhpc watchdog')
if __name__ == '__main__': raise SystemExit(main())
