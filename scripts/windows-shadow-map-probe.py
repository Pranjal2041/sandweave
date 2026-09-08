#!/usr/bin/env python3
"""Measure VMA fragmentation for a userspace guest-page-table implementation.

Uses only this process's reserved mappings and an 8 MiB sparse memfd. Also
checks unprivileged, userspace-only userfaultfd availability. Does not run a VM.
"""
import ctypes as c
import fcntl
import json
import os
from pathlib import Path
import struct
import time

if os.uname().machine != 'x86_64':
    raise SystemExit('This probe uses the Linux x86-64 syscall numbers')
lib = c.CDLL(None, use_errno=True)
lib.mmap.restype = c.c_void_p
lib.mmap.argtypes = [c.c_void_p, c.c_size_t, c.c_int, c.c_int, c.c_int, c.c_long]
lib.munmap.argtypes = [c.c_void_p, c.c_size_t]
lib.syscall.restype = c.c_long
FAILED = c.c_void_p(-1).value
PAGE, PAGES = 4096, 2048
fd = lib.syscall(c.c_long(319), c.c_char_p(b'windows-shadow-pages'), c.c_uint(1))
if fd < 0:
    raise OSError(c.get_errno(), 'memfd_create')
os.ftruncate(fd, PAGES * PAGE)


def maps_in(start, size):
    result = 0
    for row in Path('/proc/self/maps').read_text().splitlines():
        begin, end = [int(s, 16) for s in row.split()[0].split('-')]
        result += int(begin < start + size and end > start)
    return result


results = []
for permuted in [False, True]:
    size = PAGES * PAGE
    start = lib.mmap(None, size, 0, 0x22, -1, 0)  # Reserve our own range.
    if start == FAILED:
        raise OSError(c.get_errno(), 'reserve mapping')
    try:
        t = time.perf_counter_ns()
        for page in range(PAGES):
            physical = (page * 109) % PAGES if permuted else page
            address = start + page * PAGE
            # MAP_FIXED replaces only an explicitly reserved page belonging to us.
            got = lib.mmap(address, PAGE, 3, 0x11, fd, physical * PAGE)
            if got != address:
                raise OSError(c.get_errno(), f'map guest page {page}')
        elapsed = time.perf_counter_ns() - t
        for page in [0, 1, 17, PAGES - 1]:
            physical = (page * 109) % PAGES if permuted else page
            c.c_uint64.from_address(start + page * PAGE).value = page + 42
            assert struct.unpack('<Q', os.pread(fd, 8, physical * PAGE))[0] == page + 42
        results.append({'permuted': permuted, 'guest_pages': PAGES,
                        'host_vmas': maps_in(start, size), 'mapping_ns': elapsed,
                        'alias_correct': True})
    finally:
        if lib.munmap(start, size):
            raise OSError(c.get_errno(), 'unmap owned range')
os.close(fd)
uffd = lib.syscall(c.c_long(323), c.c_long(os.O_CLOEXEC | os.O_NONBLOCK | 1))
uffd_result = {'user_mode_only': True, 'opened': uffd >= 0}
if uffd < 0:
    uffd_result['errno'] = c.get_errno()
else:
    try:
        api = bytearray(struct.pack('<QQQ', 0xaa, 0, 0))
        try:
            fcntl.ioctl(uffd, 0xc018aa3f, api, True)  # UFFDIO_API
            version, features, ioctls = struct.unpack('<QQQ', api)
            uffd_result.update(api=version, features=hex(features), ioctls=hex(ioctls))
        except OSError as exc:
            uffd_result['api_errno'] = exc.errno
    finally:
        os.close(uffd)
print(json.dumps({'node': os.uname().nodename,
                  'max_map_count': int(Path('/proc/sys/vm/max_map_count').read_text()),
                  'shadow_mappings': results, 'userfaultfd': uffd_result}, indent=2))
