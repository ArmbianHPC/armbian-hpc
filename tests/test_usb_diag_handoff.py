# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import io
import importlib.util
from pathlib import Path
import pytest

source=Path(__file__).parents[1]/'tools/usb_diag_handoff.py'
if not source.is_file():source=Path(__file__).with_name('usb_diag_handoff.py')
spec=importlib.util.spec_from_file_location('usb_diag_handoff',source)
handoff=importlib.util.module_from_spec(spec);spec.loader.exec_module(handoff)


class Image(io.BytesIO):
    def size(self):return len(self.getvalue())


def test_disconnect_is_accepted_only_after_tpl_confirmation():
    requests=iter([(4,0),OSError('I/O cancelled')]);writes=[];events=[];closed=[];tpl=object()
    def request():
        item=next(requests)
        if isinstance(item,Exception):raise item
        return item
    result=handoff.transfer_amlc(Image(b'abcdefgh'),request,lambda s,o,d:writes.append((s,o,d)),
             lambda:tpl,lambda:closed.append(True),lambda p,**f:events.append((p,f)))
    assert result is tpl and writes==[(0,0,b'abcd')] and closed==[True]
    assert events[-1][0]=='ram-handoff-confirmed'


def test_disconnection_without_tpl_remains_a_failure():
    def request():raise OSError('I/O cancelled')
    def wait():raise TimeoutError('still SPL')
    with pytest.raises(RuntimeError,match='0 AMLC requests'):
        handoff.transfer_amlc(Image(b'abcdefgh'),request,lambda *a:None,wait,lambda:None)


def test_out_of_range_request_is_not_sent_or_reclassified_as_success():
    calls=[]
    with pytest.raises(ValueError,match='outside'):
        handoff.transfer_amlc(Image(b'abc'),lambda:(5,0),lambda *a:calls.append('write'),
                             lambda:calls.append('wait'),lambda:None)
    assert calls==[]


def test_final_ack_disconnect_requires_tpl_as_well():
    def write(*args):raise OSError('Device reenumerated after payload')
    tpl=object()
    assert handoff.transfer_amlc(Image(b'abc'),lambda:(3,0),write,lambda:tpl,lambda:None) is tpl


def test_repeated_final_request_is_not_resent():
    writes=[];tpl=object()
    result=handoff.transfer_amlc(Image(b'abc'),lambda:(3,0),lambda *a:writes.append(a),lambda:tpl,lambda:None)
    assert result is tpl and len(writes)==1


def test_reenumeration_skips_spl_and_waits_for_tpl():
    states=iter([(1,8),(0,16)]);closed=[]
    device=handoff.wait_for_tpl(lambda:object(),lambda d:next(states),lambda d:closed.append(d),sleep=lambda x:None)
    assert device is not None and len(closed)==1


def test_reenumeration_timeout_is_bounded():
    count=[0]
    def clock():count[0]+=1;return count[0]
    with pytest.raises(TimeoutError,match='TPL was not observed'):
        handoff.wait_for_tpl(lambda:object(),lambda d:(1,8),lambda d:None,timeout=3,clock=clock,sleep=lambda x:None)
