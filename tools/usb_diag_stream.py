# SPDX-FileCopyrightText: 2026 ARMHPC contributors
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""Read a bounded USB bulk input stream without assuming one full host packet."""
import time


class USBReadError(ValueError):
    def __init__(self, message, transfer):
        super().__init__(message)
        self.transfer = transfer


def read_exact_bulk(read, length, progress=None, chunk_size=4096, timeout_ms=10000,
                    max_calls=8192, max_empty=4, clock=time.monotonic):
    if not 0 < length <= 1024**2 or not 0 < chunk_size <= 4096:
        raise ValueError('Invalid bounded USB read size')
    received = bytearray()
    calls = short_reads = empty_reads = consecutive_empty = 0
    deadline = clock() + timeout_ms / 1000

    def snapshot():
        return {'expected_bytes': length, 'received_bytes': len(received),
                'read_calls': calls, 'short_reads': short_reads,
                'zero_length_packets': empty_reads, 'chunk_limit': chunk_size}

    def fail(message, cause=None):
        state = snapshot()
        if progress is not None:
            progress('usb-read-incomplete', **state, reason=message)
        raise USBReadError(message, state) from cause

    while len(received) < length:
        remaining_ms = int((deadline - clock()) * 1000)
        if remaining_ms <= 0:
            fail('USB input did not reach its declared length before the deadline')
        if calls >= max_calls:
            fail('USB input exceeded the bounded read-call budget')
        wanted = min(chunk_size, length - len(received))
        calls += 1
        try:
            part = bytes(read(wanted, remaining_ms))
        except Exception as error:
            fail('USB bulk input failed before its declared length (' + type(error).__name__ + ')', error)
        if len(part) > wanted:
            fail('USB input returned more bytes than requested')
        if not part:
            empty_reads += 1
            consecutive_empty += 1
            if consecutive_empty > max_empty:
                fail('USB input repeatedly returned zero-length packets before completion')
            continue
        consecutive_empty = 0
        if len(part) < wanted:
            short_reads += 1
        received.extend(part)
    if progress is not None:
        progress('usb-read-completed', **snapshot())
    return bytes(received)
