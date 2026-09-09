"""Per-request startup measurements, isolated across concurrent worker threads."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_current = ContextVar('sandweave_startup_timings', default=None)


@contextmanager
def collect():
    values = {}
    token = _current.set(values)
    try:
        yield values
    finally:
        _current.reset(token)


@contextmanager
def measure(name):
    values = _current.get()
    if values is None:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        values[name] = values.get(name, 0.0) + time.monotonic() - started
