#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Assemble the private OESP MBR/FAT32 image through the common A/B pipeline."""
from pathlib import Path
import runpy,sys
if __name__=='__main__':
    if '--profile' in sys.argv:raise SystemExit('OESP profile is fixed by this entrypoint')
    sys.argv += ['--profile','wxy-oesp-v22']
    runpy.run_path(str(Path(__file__).with_name('assemble-oect-squashfs.py')),run_name='__main__')
