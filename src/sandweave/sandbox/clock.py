"""Per-sandbox guest wall clock: a host offset or a start date, and pause behavior."""
from datetime import datetime, timedelta, timezone
import math
import time

from .errors import UnsupportedFeature

PAUSE_MODES = ('freeze', 'continue')
# The engine accepts offsets and start times up to 2**62 ns (about 146 years).
LIMIT_NS = 1 << 62
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _offset_ns(value):
    if isinstance(value, timedelta):
        return (value.days * 86400 + value.seconds) * 10**9 + value.microseconds * 1000
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('clock_offset must be seconds or a datetime.timedelta')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError('clock_offset must be finite')
    return value * 10**9 if isinstance(value, int) else round(value * 1e9)


def _start_ns(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError('clock_start must be an ISO 8601 date and time with a timezone') from None
    if not isinstance(value, datetime):
        raise ValueError('clock_start must be a timezone-aware datetime or an ISO 8601 string')
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('clock_start must include a timezone, for example 2025-03-01T00:00:00Z')
    delta = value - EPOCH
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def configure(*, clock_offset=None, clock_start=None, clock_pause=None, runtime='gvisor'):
    """Return the spec's clock entry, or None for host time with running pauses.

    The entry applies when the sandbox starts or is restored. It is never taken
    from a snapshot. Setting an offset or start date freezes guest time during
    pauses unless clock_pause='continue'.
    """
    if clock_offset is not None and clock_start is not None:
        raise ValueError('clock_offset and clock_start are alternatives')
    clock = {}
    if clock_offset is not None:
        clock['offset_ns'] = _offset_ns(clock_offset)
        if not -LIMIT_NS <= clock['offset_ns'] <= LIMIT_NS:
            raise ValueError('clock_offset must be within ±146 years')
        if time.time_ns() + clock['offset_ns'] < 0:
            raise ValueError('clock_offset cannot place guest time before 1970')
    if clock_start is not None:
        clock['start_ns'] = _start_ns(clock_start)
        if not 0 <= clock['start_ns'] <= LIMIT_NS:
            raise ValueError('clock_start must be between 1970 and 2116')
    if clock_pause is not None:
        if clock_pause not in PAUSE_MODES:
            raise ValueError("clock_pause must be 'freeze' or 'continue'")
        clock['pause'] = clock_pause
    elif clock:
        clock['pause'] = 'freeze'
    if not clock:
        return None
    if runtime != 'gvisor':
        raise UnsupportedFeature('clock settings require runtime="gvisor"')
    return clock


def check_worker(spec, connection):
    if spec.get('clock') and not connection.call('ping').get('guest_clock'):
        raise UnsupportedFeature('clock settings require Sandweave 0.2.45 or newer on the worker and controller')


def runtime_options(clock):
    """Launcher options for a spec's clock entry."""
    if not clock:
        return []
    options = []
    if 'offset_ns' in clock:
        options += ['--clock-offset-ns', str(clock['offset_ns'])]
    if 'start_ns' in clock:
        options += ['--clock-start-ns', str(clock['start_ns'])]
    return options + ['--clock-pause', clock.get('pause', 'continue')]


def features(clock):
    """Engine flags a spec's clock entry requires."""
    if not clock:
        return []
    return ['clock-start' if 'start_ns' in clock else 'clock-offset', 'clock-pause']


def summary(clock):
    result = {}
    if 'offset_ns' in clock:
        result['offset_seconds'] = clock['offset_ns'] / 1e9
    if 'start_ns' in clock:
        seconds, nanoseconds = divmod(clock['start_ns'], 10**9)
        result['start'] = (EPOCH + timedelta(seconds=seconds, microseconds=nanoseconds // 1000)).isoformat()
    result['pause'] = clock.get('pause', 'continue')
    return result
