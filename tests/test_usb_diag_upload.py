# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
import importlib.util
from pathlib import Path
import struct
import sys
import pytest

source=Path(__file__).parents[1]/'tools/usb_diag_upload.py'
if not source.is_file():source=Path(__file__).with_name('usb_diag_upload.py')
sys.path.insert(0,str(source.parent))
spec=importlib.util.spec_from_file_location('usb_diag_upload',source)
upload=importlib.util.module_from_spec(spec);spec.loader.exec_module(upload)


def header(size,tag=0xefe8):return struct.pack('<IIII',tag,size,0,0)


def test_upload_uses_each_header_length_and_returns_exact_payload():
    payload=bytes(range(256))*300;remaining=bytearray(payload)
    headers=iter([header(65536),header(len(payload)-65536)])
    events=[]
    def read(wanted,timeout):
        count=min(512,wanted,len(remaining));part=bytes(remaining[:count]);del remaining[:count]
        return part
    result=upload.receive_upload(len(payload),lambda:next(headers),read,lambda p,**f:events.append((p,f)))
    assert result==payload and not remaining
    assert events[-1][1]['packet_lengths']==[65536,len(payload)-65536]
    assert events[-1][1]['upload_packets']==2
    assert events[-1][1]['short_reads']>0


@pytest.mark.parametrize('descriptor',[b'bad',header(0),header(513),header(512,0x1234)])
def test_invalid_upload_header_never_enters_bulk_receive(descriptor):
    with pytest.raises(upload.USBReadError):
        upload.receive_upload(512,lambda:descriptor,lambda *a:pytest.fail('Unexpected bulk input'))


def test_too_small_nonfinal_packet_is_rejected_before_bulk_input():
    with pytest.raises(upload.USBReadError,match='final TPL'):
        upload.receive_upload(65537,lambda:header(512),lambda *a:pytest.fail('Unexpected bulk input'))


def test_failure_in_second_packet_preserves_total_received_count():
    headers=iter([header(65536),header(512)])
    left=[65536];last=[False]
    def read(wanted,timeout):
        if left[0]:left[0]-=wanted;return b'x'*wanted
        if not last[0]:last[0]=True;return b'y'*128
        raise OSError('USB disconnected')
    with pytest.raises(upload.USBReadError) as error:
        upload.receive_upload(66048,lambda:next(headers),read)
    assert error.value.transfer['received_bytes']==65536+128
    assert error.value.transfer['packet_received_bytes']==128
    assert isinstance(error.value.__cause__,OSError)


def test_single_small_packet_does_not_require_padding_or_an_extra_header():
    count=[0]
    def control():count[0]+=1;return header(4)
    assert upload.receive_upload(4,control,lambda *a:b'abcd')==b'abcd'
    assert count==[1]
