# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import copy
import json
from pathlib import Path
import unittest

from framework.boot_chain import reject_failed_candidate
from framework.config import ConfigError


class BootCompatibilityTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'adapters/boards/wxy-oect-original/boot/compatibility.json'
        self.record = json.loads(path.read_text())
        self.components = self.record['rejected_candidates'][0]['components']

    def test_reported_maskrom_pair_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, 'MaskROM'):
            reject_failed_candidate(self.record, self.components)

    def test_different_pair_is_not_marked_as_the_reported_failure(self):
        changed = {**self.components, 'uboot': '0' * 64}
        reject_failed_candidate(self.record, changed)

    def test_empty_rejection_cannot_match_every_candidate(self):
        malformed = copy.deepcopy(self.record)
        malformed['rejected_candidates'][0]['components'] = {}
        with self.assertRaises(ConfigError):
            reject_failed_candidate(malformed, self.components)


if __name__ == '__main__':
    unittest.main()
