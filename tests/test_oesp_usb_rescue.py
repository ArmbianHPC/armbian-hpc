# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import hashlib
import importlib.util
from pathlib import Path
import sys
from unittest.mock import Mock
import pytest

tools=Path(__file__).parents[1]/'tools'
sys.path.insert(0,str(tools))
spec=importlib.util.spec_from_file_location('usb_rescue',tools/'oesp-usb-rescue.py')
rescue=importlib.util.module_from_spec(spec);spec.loader.exec_module(rescue)


def channel():
    device=Mock();device.bulkCmdStat.return_value=b'success';device.devRead.return_value=b'OK!!'
    progress=Mock();value=rescue.RAMChannel(device,[(0x08080000,200000)],progress)
    return value,device,progress


def test_ram_upload_bound_chunks_and_readback_before_handoff():
    value,device,progress=channel();data=b'kernel'*20000
    value.read=Mock(side_effect=lambda address,length:data[address-0x08080000:address-0x08080000+length])
    value.write(0x08080000,data,'kernel')
    calls=device.writeMedia.call_args_list
    assert [len(c.args[0]) for c in calls]==[65536,len(data)-65536]
    assert [c.kwargs['seq'] for c in calls]==[0,1]
    assert all(c.kwargs['retryTimes']==0 for c in calls)
    assert 'download mem' in device.dev.ctrl_transfer.call_args.kwargs['data_or_wLength']
    assert any(c.args[0]=='ram-verified' for c in progress.call_args_list)
    assert not any('booti' in c.kwargs['data_or_wLength'] for c in device.dev.ctrl_transfer.call_args_list)


@pytest.mark.parametrize('failure',['short-write','bad-ack','bad-readback'])
def test_ram_errors_stop_before_boot_and_do_not_reissue_unknown_write(failure):
    value,device,_=channel();value.read=Mock(return_value=b'changed')
    if failure=='short-write':device.writeMedia.return_value=False
    if failure=='bad-ack':device.devRead.return_value=b'failed:'
    with pytest.raises(ValueError):value.write(0x08080000,b'payload','kernel')
    assert device.writeMedia.call_count==1
    assert not any('booti' in c.kwargs['data_or_wLength'] for c in device.dev.ctrl_transfer.call_args_list)


@pytest.mark.parametrize('command',['mmc write 8080000 0 1','saveenv','efuse write 0 1',
    'download store 1 normal 0x100','download mem 0x08080000 normal 0x40000',
    'upload mem 0x00000000 normal 0x100'])
def test_allowlist_refuses_storage_register_and_out_of_payload_writes(command):
    value,device,_=channel()
    with pytest.raises(ValueError):value.command(command)
    device.dev.ctrl_transfer.assert_not_called()


def test_handoff_has_no_status_poll_and_boot_arguments_are_bounded():
    value,device,_=channel()
    value.command('setenv bootargs ""')
    value.command('setenv bootargs "${bootargs} armhpc.recovery-slot=a"')
    device.bulkCmdStat.reset_mock()
    value.command('booti 0x08080000 0x13000000 0x08008000',handoff=True)
    device.bulkCmdStat.assert_not_called()
