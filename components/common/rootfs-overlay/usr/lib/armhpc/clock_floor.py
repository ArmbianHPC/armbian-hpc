# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only
"""RTC-less clock recovery, durable checkpoints and explicit time quality."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time

BUILD = Path('/etc/armhpc/image-build-time.json')
SAVED = Path('/var/lib/armhpc/clock-floor.json')
RESUME = Path('/var/lib/armhpc/clock-resume.json')
STATUS = Path('/run/armhpc/clock-status.json')
LOCK = Path('/run/armhpc/clock.lock')
SHARED_ROOT = Path('/var/lib/armhpc/ota')
SHARED = SHARED_ROOT / 'clock/confirmed-time.json'
SHARED_RESUME = SHARED_ROOT / 'clock/resume-time.json'
PERSIST_INTERVAL = 300


class ClockError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def load(path):
    try:
        if Path(path).is_symlink(): return {}
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save(path, value):
    path = Path(path)
    if path.is_symlink(): raise ClockError('clock_path', 'Clock record cannot be a symbolic link')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(value, indent=2, allow_nan=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def epoch(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value < 253402300800


def floor_epoch(build, saved):
    values = [item.get('epoch', 0) for item in (build, saved)]
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in values):
        raise ValueError('Invalid clock floor metadata')
    return max(values)


def advance(current, floor):
    if floor and current < floor:
        time.clock_settime(time.CLOCK_REALTIME, float(floor))
        return True
    return False


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def shared_clock_path():
    if not SHARED_ROOT.is_mount() or (SHARED_ROOT / 'clock').is_symlink(): return None
    try:
        result = subprocess.run(['findmnt', '-n', '-o', 'FSTYPE,LABEL', '--mountpoint', str(SHARED_ROOT)],
                                text=True, capture_output=True, timeout=2)
        return SHARED if result.returncode == 0 and result.stdout.split() == ['ext4', 'OTA_SHARED'] else None
    except (OSError, subprocess.TimeoutExpired): return None


@contextmanager
def clock_lock():
    import fcntl
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a+b') as stream:
        deadline = time.monotonic() + 1
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline: raise ClockError('clock_busy', 'Another clock checkpoint is running')
                time.sleep(0.02)
        yield


def anchor_id(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def records():
    shared = shared_clock_path()
    warnings = [] if shared else ['shared_clock_unavailable']
    confirmed = []
    for path in ([SAVED, shared] if shared else [SAVED]):
        item = load(path)
        revision = item.get('revision', 0)
        if epoch(item.get('epoch')) and item.get('source') == 'chrony-confirmed-ntp' and isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
            confirmed.append(item)
        elif Path(path).exists(): warnings.append('invalid_record:' + str(path))
    # New revisions can legitimately contain an earlier NTP-corrected epoch.
    chosen = max(confirmed, key=lambda v: (v.get('revision', 0), v['epoch'])) if confirmed else None
    build = load(BUILD)
    base = chosen or (build if epoch(build.get('epoch')) else {'epoch': 0, 'source': 'unavailable'})
    identity = anchor_id(base)
    resumes = []
    for path in ([RESUME, SHARED_RESUME] if shared else [RESUME]):
        item = load(path)
        if epoch(item.get('epoch')) and item.get('anchor_id') == identity and item['epoch'] >= base['epoch']:
            resumes.append(item)
        elif Path(path).exists(): warnings.append('invalid_resume:' + str(path))
    floor = max([base['epoch']] + [v['epoch'] for v in resumes])
    return {'shared': shared, 'confirmed': chosen, 'anchor_id': identity,
            'floor': floor, 'valid_anchor': bool(chosen), 'warnings': warnings}


def tracking(timeout=2):
    try:
        result = subprocess.run(['chronyc', '-c', 'tracking'], text=True, capture_output=True, timeout=max(0.05, timeout))
        fields = result.stdout.strip().split(',')
        # chronyc CSV includes both the reference ID and source address.
        # Reference, address, stratum, reference epoch, correction, ... , leap.
        if result.returncode != 0 or len(fields) != 14: return None
        reference = fields[0].strip().upper()
        if len(reference) != 8 or any(c not in '0123456789ABCDEF' for c in reference): return None
        stratum, reference_time, correction = int(fields[2]), float(fields[3]), float(fields[4])
        if reference in ('00000000', '7F7F0101') or not 1 <= stratum <= 15 or not epoch(reference_time): return None
        if fields[13].strip() != 'Normal' or not math.isfinite(correction) or abs(correction) > 1: return None
        return {'reference': reference, 'stratum': stratum, 'remaining_correction_seconds': correction}
    except (OSError, ValueError, subprocess.TimeoutExpired): return None


def runtime():
    value = load(STATUS)
    if value.get('boot_id') != boot_id(): return {}
    if not isinstance(value.get('valid_anchor'), bool) or value.get('quality') not in ('synchronised', 'estimated', 'invalid'): return {}
    for name in ('anchor_monotonic', 'checked_monotonic', 'persisted_monotonic'):
        if name in value and (not isinstance(value[name], (int, float)) or isinstance(value[name], bool) or not math.isfinite(value[name]) or value[name] < 0): return {}
    if not isinstance(value.get('anchor_id'), str) or not epoch(value.get('anchor_epoch')): return {}
    return value


def derived(value, monotonic=None):
    monotonic = time.monotonic() if monotonic is None else monotonic
    base, start = value.get('anchor_epoch'), value.get('anchor_monotonic')
    if not epoch(base) or not isinstance(start, (int, float)) or not math.isfinite(start) or start > monotonic:
        return None
    return base + monotonic - start


def _restore(info):
    previous = runtime()
    if previous.get('anchor_id') == info['anchor_id'] and derived(previous) is not None:
        return previous
    before, monotonic = time.time(), time.monotonic()
    changed = advance(before, info['floor'])
    state = {'schema_version': 2, 'boot_id': boot_id(), 'quality': 'estimated' if info['valid_anchor'] else 'invalid',
             'source': 'restored-shared-time' if info['shared'] else 'restored-local-time',
             'ntp_synchronised': False, 'valid_anchor': info['valid_anchor'], 'anchor_id': info['anchor_id'],
             'anchor_epoch': info['floor'], 'anchor_monotonic': monotonic,
             'last_saved_epoch': info['floor'] or None, 'boot_floor_epoch': info['floor'],
             'clock_before': before, 'advanced_to_floor': changed, 'warnings': info['warnings']}
    save(STATUS, state)
    return state


def restore():
    with clock_lock(): return _restore(records())


def snapshot(live=False):
    state = runtime()
    if not state:
        info = records()
        if STATUS.exists(): info['warnings'].append('invalid_runtime_clock_status')
        state = {'quality': 'estimated' if info['valid_anchor'] else 'invalid', 'source': 'saved-time-only',
                 'valid_anchor': info['valid_anchor'], 'last_saved_epoch': info['floor'] or None,
                 'ntp_synchronised': False, 'boot_id': boot_id(), 'warnings': info['warnings']}
    result = {k: state[k] for k in ('quality', 'source', 'valid_anchor', 'last_saved_epoch', 'boot_id', 'warnings', 'correction') if k in state}
    expected = derived(state)
    fresh = time.monotonic() - state.get('checked_monotonic', -1000) <= 60
    synchronised = bool(state.get('ntp_synchronised') and fresh and expected is not None and abs(time.time() - expected) <= 1)
    if live: synchronised = tracking() is not None
    result['ntp_synchronised'] = synchronised
    result['quality'] = 'synchronised' if synchronised else ('estimated' if state.get('valid_anchor') else 'invalid')
    result['epoch'] = time.time()
    result['estimated_epoch'] = expected
    if live and synchronised: result['valid_anchor'] = True; result['source'] = 'chrony-confirmed-ntp'
    return result


def checkpoint(force=False, reason='periodic', require_anchor=False, ntp_timeout=2):
    with clock_lock():
        info = records()
        state = _restore(info)
        ntp = tracking(ntp_timeout)
        monotonic = time.monotonic()
        expected = derived(state, monotonic)
        if ntp:
            current = time.time()
            first = not state.get('ntp_synchronised')
            correction = current - expected if expected is not None else 0
            if abs(correction) > 1:
                state['correction'] = {'id': hashlib.sha256((boot_id() + ':' + str(monotonic)).encode()).hexdigest()[:16],
                                       'delta_seconds': correction, 'direction': 'forward' if correction > 0 else 'backward'}
            persist = force or first or abs(correction) > 1 or monotonic - state.get('persisted_monotonic', -PERSIST_INTERVAL) >= PERSIST_INTERVAL
            if persist:
                value = {'schema_version': 2, 'epoch': int(current), 'source': 'chrony-confirmed-ntp',
                         'revision': (info['confirmed'] or {}).get('revision', 0) + 1, 'boot_id': boot_id(),
                         'monotonic_seconds': monotonic, 'reason': reason}
                # Authoritative shared revision first. Old resumes no longer match its anchor.
                if info['shared']: save(info['shared'], value)
                save(SAVED, value)
                state['anchor_id'] = anchor_id(value)
                state['persisted_monotonic'] = monotonic
                state['last_saved_epoch'] = current
            state.update(quality='synchronised', source='chrony-confirmed-ntp', ntp_synchronised=True,
                         valid_anchor=True, anchor_epoch=current, anchor_monotonic=monotonic,
                         checked_monotonic=monotonic)
        else:
            state.update(quality='estimated' if state.get('valid_anchor') else 'invalid',
                         source='estimated-monotonic' if state.get('valid_anchor') else 'build-time-only', ntp_synchronised=False)
            if require_anchor and not state.get('valid_anchor'):
                save(STATUS, state)
                raise ClockError('clock_state_invalid', 'No calibrated or recoverable clock anchor; synchronize NTP before starting OTA')
            persist = force or monotonic - state.get('persisted_monotonic', -PERSIST_INTERVAL) >= PERSIST_INTERVAL
        if persist and state.get('valid_anchor'):
            resume = {'schema_version': 2, 'epoch': derived(state, monotonic), 'anchor_id': state['anchor_id'],
                      'source': state['source'], 'boot_id': boot_id(), 'monotonic_seconds': monotonic, 'reason': reason}
            if info['shared']: save(SHARED_RESUME, resume)
            save(RESUME, resume)
            state['last_saved_epoch'] = resume['epoch']
            state['persisted_monotonic'] = monotonic
        state['warnings'] = info['warnings']
        save(STATUS, state)
    return snapshot()


def wait_sync(timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        result = checkpoint(force=False, reason='time-readiness', ntp_timeout=min(2, max(0.05, remaining)))
        if result['ntp_synchronised'] or remaining <= 0 or time.monotonic() >= deadline: return result
        time.sleep(min(1, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--checkpoint', action='store_true')
    group.add_argument('--flush', action='store_true')
    group.add_argument('--status', action='store_true')
    group.add_argument('--wait-sync', action='store_true')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--reason', default='periodic')
    args = parser.parse_args()
    if os.geteuid() != 0: parser.error('Run as root')
    if not 0 <= args.timeout <= 30: parser.error('Timeout must be between 0 and 30 seconds')
    try:
        if args.status: result = snapshot(live=True)
        elif args.wait_sync: result = wait_sync(args.timeout)
        elif args.checkpoint or args.flush: result = checkpoint(args.flush, args.reason, args.flush, 1 if args.flush else 2)
        else: restore(); result = snapshot()
        print(json.dumps({'protocol': 1, 'ok': True, 'data': result}, allow_nan=False))
        return 0
    except (ClockError, OSError, ValueError) as error:
        print(json.dumps({'protocol': 1, 'ok': False, 'error': {'code': getattr(error, 'code', 'clock_io'), 'message': str(error)}}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
