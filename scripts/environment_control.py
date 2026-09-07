"""Shared host-side lifecycle locking and CPU-controller coordination."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import time

LOCK_FD_ENV = '_GENERAL_VM_OPERATION_LOCK_FD'


def valid_name(name):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
        raise ValueError('names must use letters, digits, dash or underscore')
    return name


def acquire_lock(local, name, inherited_fd=None):
    valid_name(name)
    directory = Path(local) / 'gvisor/operations'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (name + '.lock')
    if inherited_fd is None:
        handle = path.open('a+')
    else:
        handle = os.fdopen(os.dup(int(inherited_fd)), 'a+')
        if not os.path.samestat(os.fstat(handle.fileno()), path.stat()):
            handle.close()
            raise ValueError('inherited operation lock belongs to another environment')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError(f'{name}: another lifecycle or snapshot operation is in progress') from None
    return handle


def release_cpu(local, name):
    """Release CPU throttling; return only the markers this call created."""
    created = []
    try:
        for registration in (Path(local) / 'gvisor/cpu-brokers').glob('*/job-' + valid_name(name) + '.json'):
            marker = registration.with_name(registration.name + '.suspend')
            try:
                with marker.open('x') as output:
                    output.write('host lifecycle operation or user pause\n')
                created.append(marker)
            except FileExistsError:
                pass
            deadline = time.monotonic() + 5
            while True:
                try:
                    status = json.loads((registration.parent / 'status.json').read_text())
                    job = status['jobs'].get(registration.name)
                    if job and not job['paused'] and status['time'] > marker.stat().st_mtime:
                        break
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError('CPU controller did not release lifecycle scheduling')
                time.sleep(.05)
        return created
    except BaseException:
        for marker in created:
            marker.unlink(missing_ok=True)
        raise


def resume_cpu(local, name):
    for marker in (Path(local) / 'gvisor/cpu-brokers').glob('*/job-' + valid_name(name) + '.json.suspend'):
        marker.unlink(missing_ok=True)


@contextmanager
def suspended_cpu(local, name):
    created = release_cpu(local, name)
    try:
        yield
    finally:
        for marker in created:
            marker.unlink(missing_ok=True)
