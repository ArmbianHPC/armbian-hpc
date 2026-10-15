# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Bounded G12 AMLC handoff; only an observed TPL makes a disconnect successful."""
import time


def emit(callback,phase,**fields):
    if callback is not None:callback(phase,**fields)


def transfer_amlc(image,request,write,wait_tpl,close,progress=None,clock=time.monotonic,
                  timeout=180,max_requests=128,transport_errors=(OSError,)):
    previous=None
    sent=0
    sequence=0
    deadline=clock()+timeout
    size=image.size()
    def handoff(reason,error=None):
        emit(progress,'ram-handoff-wait',reason=reason,requests_sent=sequence,bytes_sent=sent,
             last_request=list(previous) if previous else None,error=str(error) if error else None)
        try:close()
        except Exception as exc:emit(progress,'old-usb-handle-release',error=str(exc))
        try:device=wait_tpl()
        except Exception as exc:
            raise RuntimeError('SPL to TPL handoff failed after '+str(sequence)+' AMLC requests: '+str(exc)) from error or exc
        emit(progress,'ram-handoff-confirmed',requests_sent=sequence,bytes_sent=sent)
        return device
    while sequence<max_requests and clock()<deadline:
        emit(progress,'amlc-request',amlc_sequence=sequence)
        try:length,offset=request()
        except transport_errors as exc:return handoff('USB interruption while requesting AMLC',exc)
        need=0<length<=size and 0<=offset<=size-length
        if not need:raise ValueError('AMLC request lies outside the pinned FIP')
        if previous==(length,offset):return handoff('Repeated final AMLC request')
        previous=(length,offset)
        image.seek(offset,0);data=image.read(length)
        if len(data)!=length:raise ValueError('Pinned FIP is truncated')
        emit(progress,'amlc-write',amlc_sequence=sequence,offset=offset,bytes=length)
        try:write(sequence,offset,data)
        except transport_errors as exc:return handoff('USB interruption during AMLC data or acknowledgement',exc)
        emit(progress,'amlc-acknowledged',amlc_sequence=sequence,offset=offset,bytes=length)
        sent+=length;sequence+=1
    raise TimeoutError('AMLC loading exceeded its bounded request/time budget')


def wait_for_tpl(open_device,inspect,close,progress=None,timeout=35,clock=time.monotonic,sleep=time.sleep):
    deadline=clock()+timeout
    last=None
    while clock()<deadline:
        device=None
        try:
            device=open_device()
            state=inspect(device)
            emit(progress,'usb-reenumeration',state=state)
            if state[0]==0 and state[1]==16:return device
            last='Observed stage '+str(state)
        except (OSError,ValueError) as exc:last=str(exc)
        if device is not None:
            try:close(device)
            except Exception:pass
        sleep(.5)
    raise TimeoutError('TPL was not observed within '+str(timeout)+' seconds; '+str(last))
