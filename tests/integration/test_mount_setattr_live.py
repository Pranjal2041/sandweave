"""Check unsupported mount_setattr calls cannot retain guest mount references."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import time

import pytest

from sandweave import Memory, Sandbox
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit isolated worker required')]

# Run only inside the disposable guest: ordinary unmount is the reference-leak
# oracle. MNT_DETACH would hide a leaked reference and is deliberately not used.
PROBE = r'''
import ctypes as c
import errno
import json
import os
import tempfile

libc = c.CDLL(None, use_errno=True)
libc.syscall.restype = c.c_long
libc.mount.argtypes = [c.c_char_p, c.c_char_p, c.c_char_p, c.c_ulong, c.c_char_p]
libc.umount2.argtypes = [c.c_char_p, c.c_int]

class MountAttr(c.Structure):
    _fields_ = [(name, c.c_uint64) for name in ('set', 'clear', 'propagation', 'userns_fd')]

attr = MountAttr(1, 0, 0, 0)  # MOUNT_ATTR_RDONLY
calls = 0
def unsupported(fd, path, flags=0):
    global calls
    c.set_errno(0)
    result = libc.syscall(c.c_long(442), c.c_int(fd), path, c.c_uint(flags),
                          c.byref(attr), c.c_size_t(c.sizeof(attr)))
    assert result == -1 and c.get_errno() == errno.ENOSYS, (result, c.get_errno())
    calls += 1

# An absent pathname, invalid fd and unreadable pathname pointer all return
# ENOSYS, rather than doing path/fd validation first.
unsupported(-100, c.c_char_p(b'/no-such-mount-setattr-path'))
unsupported(-12345, c.c_char_p(b'relative'))
unsupported(-100, c.c_void_p(1))

with tempfile.TemporaryDirectory(prefix='mount-setattr-') as target:
    path = os.fsencode(target)
    for iteration in range(32):
        assert libc.mount(b'tmpfs', path, b'tmpfs', 0, b'size=1m') == 0, c.get_errno()
        fd = os.open(target, os.O_PATH | os.O_DIRECTORY)
        try:
            if iteration == 0:
                # Verify that this runtime really detects outstanding refs.
                assert libc.umount2(path, 0) == -1 and c.get_errno() == errno.EBUSY
            for _ in range(32):
                unsupported(-100, c.c_char_p(path))
                unsupported(-100, c.c_char_p(path), 0x8000)  # AT_RECURSIVE
                unsupported(fd, c.c_char_p(b'.'))
                unsupported(fd, c.c_char_p(b''), 0x1000)  # AT_EMPTY_PATH
        finally:
            os.close(fd)
        assert libc.umount2(path, 0) == 0, ('ordinary unmount', iteration, c.get_errno())
print(json.dumps({'calls': calls, 'ordinary_unmounts': 32, 'held_fd_ebusy': True}))
'''


def test_failed_mount_setattr_keeps_mounts_releasable_under_concurrency():
    guests = []
    try:
        for _ in range(5):
            guests.append(Sandbox(memory=Memory('256MiB', '256MiB')))
        with ThreadPoolExecutor(max_workers=4) as executor:
            pending = [executor.submit(env.run, argv=['python3', '-c', PROBE],
                                       timeout=60, check=True) for env in guests[:4]]
            peer_latencies = []
            while not all(future.done() for future in pending):
                start = time.monotonic()
                assert guests[4].run('printf alive', timeout=5).stdout == 'alive'
                peer_latencies.append(time.monotonic() - start)
                time.sleep(.02)
            results = [json.loads(future.result().stdout) for future in pending]
        assert all(result['calls'] == 4099 and result['ordinary_unmounts'] == 32
                   and result['held_fd_ebusy'] for result in results)
        print(json.dumps({'guests': results, 'peer_max_seconds': max(peer_latencies, default=0)}))
    finally:
        for env in guests:
            env.terminate()
            env.close()
        connection = local_connection()
        connection.call('_shutdown_if_idle')
        connection.close()
