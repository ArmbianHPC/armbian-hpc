# SPDX-License-Identifier: GPL-2.0-only
"""Use the independent OTA project's installer to prepare an offline release root."""
import argparse
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--ota-source', required=True)
parser.add_argument('--root', required=True)
parser.add_argument('--watchdog-binary', required=True)
args = parser.parse_args()
source = Path(args.ota_source).resolve()
installer = source/'tools/install-runtime.py'
if not installer.is_file(): parser.error('Missing independent armhpc-ota source checkout')
subprocess.run([sys.executable, str(installer), '--root', args.root, '--watchdog-binary', args.watchdog_binary], check=True)
