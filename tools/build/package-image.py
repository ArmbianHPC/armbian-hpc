#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Provide IMG.XZ, SHA256SUMS and a full decompression verification report."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from framework.artifacts import package_image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--expected-sha256')
    parser.add_argument('--threads', type=int, default=2)
    args = parser.parse_args()
    report = package_image(args.image, expected_sha256=args.expected_sha256, threads=args.threads)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
