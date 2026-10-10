# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.machinery
import importlib.util
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
loader=importlib.machinery.SourceFileLoader('rescue_menu_tests',str(ROOT/'components/rescue/rootfs-overlay/usr/lib/armhpc/rescue_menu.py'))
spec=importlib.util.spec_from_loader(loader.name,loader)
menu=importlib.util.module_from_spec(spec)
loader.exec_module(menu)

class MenuTests(unittest.TestCase):
    def choose(self, answer):
        output=json.dumps({'blockdevices':[
            {'path':'/dev/sda','type':'disk','tran':'sata','size':240057409536,'model':'fixture','serial':'fixture'},
            {'path':'/dev/mmcblk0','type':'disk','tran':None,'size':8000000000}]})
        with mock.patch.object(menu.subprocess,'run',return_value=SimpleNamespace(stdout=output)), \
             mock.patch('builtins.input',return_value=answer),redirect_stdout(io.StringIO()):
            return menu.select_sata_disk()

    def test_number_and_exact_disk_path_select_the_same_sata(self):
        self.assertEqual(self.choose('1'),'/dev/sda')
        self.assertEqual(self.choose('/dev/sda'),'/dev/sda')

    def test_invalid_truncated_partition_and_emmc_inputs_are_rejected(self):
        for answer in ('/dv','/dev/sda1','/dev/mmcblk0','2',''):
            with self.subTest(answer=answer):
                self.assertIsNone(self.choose(answer))
