# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import hashlib
import importlib.util
from pathlib import Path
import pytest

source=Path(__file__).parents[1]/'tools/usb_diag_stream.py'
if not source.is_file():source=Path(__file__).with_name('usb_diag_stream.py')
spec=importlib.util.spec_from_file_location('usb_diag_stream',source)
stream=importlib.util.module_from_spec(spec);spec.loader.exec_module(stream)


def test_short_reads_and_packet_boundaries_preserve_all_payload_bytes():
    payload=bytes(range(256))*40
    remaining=bytearray(payload);calls=[];events=[]
    sizes=iter([0,17,0,512,0,73,4096,251,4096,1195])
    def read(wanted,timeout):
        assert 0 < timeout <= 10000
        count=min(next(sizes,4096),wanted,len(remaining))
        part=bytes(remaining[:count]);del remaining[:count]
        calls.append(wanted);return part
    result=stream.read_exact_bulk(read,len(payload),lambda p,**f:events.append((p,f)))
    assert hashlib.sha256(result).digest()==hashlib.sha256(payload).digest()
    assert not remaining and max(calls)<=4096
    assert events[-1][1]['short_reads']>0 and events[-1][1]['zero_length_packets']==3


def test_endless_empty_reads_remain_an_incomplete_capture():
    with pytest.raises(stream.USBReadError,match='zero-length') as error:
        stream.read_exact_bulk(lambda *a:b'',512)
    assert error.value.transfer['expected_bytes']==512
    assert error.value.transfer['received_bytes']==0
    assert error.value.transfer['read_calls']==5


def test_truncated_payload_reports_the_exact_received_count():
    chunks=iter([b'abc',b'def'])
    def read(*args):
        try:return next(chunks)
        except StopIteration:raise OSError('USB disconnected')
    with pytest.raises(stream.USBReadError,match='OSError') as error:
        stream.read_exact_bulk(read,512)
    assert error.value.transfer['received_bytes']==6
    assert isinstance(error.value.__cause__,OSError)


def test_more_bytes_than_requested_are_not_truncated_into_success():
    with pytest.raises(stream.USBReadError,match='more bytes'):
        stream.read_exact_bulk(lambda *a:b'x'*513,512)


def test_deadline_stops_a_stream_without_padding_missing_bytes():
    time=[0]
    def clock():time[0]+=1;return time[0]
    with pytest.raises(stream.USBReadError,match='deadline') as error:
        stream.read_exact_bulk(lambda *a:b'x',512,timeout_ms=2000,clock=clock)
    assert error.value.transfer['received_bytes']<512


@pytest.mark.parametrize('length',[0,1024**2+1])
def test_out_of_range_stream_never_calls_usb(length):
    with pytest.raises(ValueError,match='bounded'):
        stream.read_exact_bulk(lambda *a:pytest.fail('Unexpected USB call'),length)
