#!/usr/bin/env python3
"""Exercise Linux primitives used by the original OSWorld GDM services."""
import ctypes
import errno
import fcntl
import json
import os
import socket
import struct
import signal
import select
import stat
from pathlib import Path


def consoles():
    """Run only in a disposable sandbox with headless consoles enabled."""
    # A writable major-4 node here is supplied by the sandbox's device table.
    # Do not use the desktop's console nodes or switch a user-facing desktop.
    paths = [Path('/dev/sw-test-tty' + str(i)) for i in (0, 10, 11)]
    for path, minor in zip(paths, (0, 10, 11)):
        os.mknod(path, stat.S_IFCHR | 0o666, os.makedev(4, minor))
        os.chmod(path, 0o666)
    descriptors = [os.open(p, os.O_RDWR | os.O_NOCTTY) for p in paths]
    zero, ten, eleven = descriptors
    active = Path('/sys/class/tty/tty0/active')
    assert active.read_text() == 'tty1\n', 'probe requires an unused console bank'
    events = []
    signal.signal(signal.SIGUSR1, lambda *a: events.append('release'))
    signal.signal(signal.SIGUSR2, lambda *a: events.append('acquire'))
    with active.open() as notification:
        notification.read()
        poll = select.poll()
        poll.register(notification, select.POLLPRI)
        fcntl.ioctl(ten, 0x5602, struct.pack('BBhhh', 1, 0, signal.SIGUSR1, signal.SIGUSR2, 0))
        fcntl.ioctl(zero, 0x5606, 10)
        assert active.read_text() == 'tty10\n'
        assert poll.poll(1000), 'console switch did not notify sysfs readers'
        fcntl.ioctl(zero, 0x5606, 11)
        assert active.read_text() == 'tty10\n', 'switch skipped process-controller release'
        assert events == ['acquire', 'release'], events
        fcntl.ioctl(ten, 0x5605, 1)
        assert active.read_text() == 'tty11\n'
    child = os.fork()
    if child == 0:
        try:
            os.setgid(1000)
            os.setuid(1000)
            for nested in (False, True):
                if nested:
                    libc = ctypes.CDLL(None, use_errno=True)
                    assert libc.unshare(0x10000000) == 0
                try:
                    fcntl.ioctl(eleven, 0x5606, 12)
                except OSError as error:
                    assert error.errno == errno.EPERM
                else:
                    raise AssertionError('unprivileged process switched another session console')
            os._exit(0)
        except BaseException:
            os._exit(1)
    assert os.waitpid(child, 0)[1] == 0
    fcntl.ioctl(ten, 0x5602, struct.pack('BBhhh', 0, 0, 0, 0, 0))
    fcntl.ioctl(zero, 0x5606, 1)
    for descriptor in descriptors:
        os.close(descriptor)
    for path in paths:
        path.unlink()


def socket_locks():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    fcntl.lockf(left, fcntl.LOCK_EX | fcntl.LOCK_NB, 0, 0, os.SEEK_CUR)
    child = os.fork()
    if child == 0:
        try:
            try:
                fcntl.lockf(left, fcntl.LOCK_EX | fcntl.LOCK_NB, 0, 0, os.SEEK_CUR)
            except OSError as error:
                assert error.errno in (errno.EACCES, errno.EAGAIN)
            else:
                raise AssertionError('cross-process lock did not exclude its contender')
            left.send(b'blocked')
            assert left.recv(16) == b'released'
            fcntl.lockf(left, fcntl.LOCK_EX | fcntl.LOCK_NB, 0, 0, os.SEEK_CUR)
            os._exit(0)
        except BaseException:
            os._exit(1)
    right.settimeout(10)
    assert right.recv(16) == b'blocked'
    fcntl.lockf(left, fcntl.LOCK_UN, 0, 0, os.SEEK_CUR)
    right.send(b'released')
    assert os.waitpid(child, 0)[1] == 0
    left.close()
    right.close()


def keyrings():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    def call(operation, first=0, second=0, third=0):
        result = libc.syscall(ctypes.c_long(250), ctypes.c_long(operation),
            ctypes.c_long(first), ctypes.c_long(second), ctypes.c_long(third), ctypes.c_long(0))
        if result < 0:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
        return result
    session = call(1)
    user = call(0, -4, 1)
    assert user == call(0, -4, 1)
    call(8, -4, -3)
    buffer = ctypes.create_string_buffer(1024)
    count = call(11, -3, ctypes.addressof(buffer), len(buffer))
    assert user in struct.unpack('<' + 'i' * (count // 4), buffer.raw[:count])
    try:
        call(8, session, user)
    except OSError as error:
        assert error.errno == errno.EDEADLK
    else:
        raise AssertionError('keyring cycle was permitted')
    call(9, user, session)
    assert call(11, session) == 0


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--consoles', action='store_true', help='also test a disposable guest console bank')
    args = parser.parse_args()
    socket_locks()
    print(json.dumps({'socket_lock_contention': 'passed'}), flush=True)
    keyrings()
    print(json.dumps({'user_keyring_links': 'passed'}), flush=True)
    if args.consoles:
        consoles()
        print(json.dumps({'console_switch_permissions_and_notifications': 'passed'}), flush=True)
