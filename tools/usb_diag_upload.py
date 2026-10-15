# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Bounded TPL upload packets, each with its device-provided length header."""
import struct
from usb_diag_stream import read_exact_bulk, USBReadError

PACKET_LIMIT=65536
MAGIC=0xefe8


def receive_upload(length, header_read, bulk_read, progress=None):
    if not 0 < length <= 1024**2:
        raise ValueError('Invalid bounded TPL upload length')
    payload=bytearray()
    packets=reads=shorts=empty=0
    sizes=[]
    tag=declared=None

    def state():
        return {'expected_bytes':length,'received_bytes':len(payload),'upload_packets':packets,
                'read_calls':reads,'short_reads':shorts,'zero_length_packets':empty,
                'request':'0x33','header_tag':hex(tag) if tag is not None else None,
                'packet_declared_bytes':declared}

    try:
        while len(payload)<length:
            if packets>=128:
                raise ValueError('TPL upload exceeded its packet budget')
            header=bytes(header_read())
            if len(header)!=16:
                raise ValueError('TPL upload header must be exactly 16 bytes; received '+str(len(header)))
            tag,declared,_,_=struct.unpack('<IIII',header)
            if tag!=MAGIC:
                raise ValueError('TPL upload header has an unsupported tag '+hex(tag))
            remaining=length-len(payload)
            if not 0 < declared <= min(PACKET_LIMIT,remaining):
                raise ValueError('TPL upload packet length is outside the requested memory range: '+str(declared))
            if remaining>PACKET_LIMIT and declared!=PACKET_LIMIT:
                raise ValueError('Only the final TPL upload packet may be smaller than 64 KiB')
            packets+=1
            if progress is not None and packets==1:
                progress('tpl-upload-header',**state())
            packet_stats={}
            def packet_progress(phase,**fields):packet_stats.update(fields)
            try:
                chunk=read_exact_bulk(bulk_read,declared,progress=packet_progress)
            except USBReadError as error:
                detail=state()
                detail['received_bytes']+=error.transfer['received_bytes']
                detail['packet_received_bytes']=error.transfer['received_bytes']
                detail['read_calls']+=error.transfer['read_calls']
                detail['short_reads']+=error.transfer['short_reads']
                detail['zero_length_packets']+=error.transfer['zero_length_packets']
                raise USBReadError(str(error),detail) from error.__cause__ or error
            payload.extend(chunk)
            reads+=packet_stats['read_calls']
            shorts+=packet_stats['short_reads']
            empty+=packet_stats['zero_length_packets']
            sizes.append(declared)
    except Exception as error:
        if isinstance(error,USBReadError):wrapped=error
        else:wrapped=USBReadError(str(error),state())
        if progress is not None:progress('usb-read-incomplete',**wrapped.transfer,reason=str(error))
        if wrapped is error:raise
        raise wrapped from error
    if progress is not None:
        progress('usb-read-completed',**state(),packet_lengths=sizes)
    return bytes(payload)
