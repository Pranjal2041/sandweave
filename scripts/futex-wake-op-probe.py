#!/usr/bin/env python3
"""Compare FUTEX_WAKE_OP signed semantics on native Linux and the guest."""
import ctypes as C
import errno
import json
import platform
import threading
import time

assert platform.machine() == 'x86_64', 'SYS_futex below is x86_64-specific'
libc = C.CDLL(None, use_errno=True)
libc.syscall.restype = C.c_long


class Timespec(C.Structure):
    _fields_ = [('sec', C.c_long), ('nsec', C.c_long)]


def futex(word, operation, value, arg4, word2, encoded):
    result = libc.syscall(C.c_long(202), C.byref(word), C.c_int(operation),
                          C.c_int(value), arg4, C.byref(word2),
                          C.c_uint32(encoded))
    return result, C.get_errno() if result == -1 else 0


def encode(op, arg, cmp, cmp_arg):
    return (op << 28) | (cmp << 24) | ((arg & 0xfff) << 12) | (cmp_arg & 0xfff)


failures = []
checks = 0
for op, arg, old, expected in [
    (0, -1, 0, 0xffffffff), (1, -1, 5, 4),
    (2, -2048, 1, 0xfffff801), (3, -1, 0xffffffff, 0),
    (4, -1, 0xffffffff, 0), (8, 31, 0, 0x80000000),
    (8, 32, 0, 1), (8, -1, 0, 0x80000000),
]:
    first, second = C.c_uint32(0), C.c_uint32(old)
    result = futex(first, 5 | 128, 0, C.c_long(0), second, encode(op, arg, 0, 0))
    checks += 1
    if result != (0, 0) or second.value != expected:
        failures.append(dict(op=op, arg=arg, result=result, value=hex(second.value)))

for private in (0, 128):
    for old, cmp_arg in [(-2147483648, 0), (-1, -1), (1, -1), (-2048, 2047)]:
        for cmp, expected in enumerate((old == cmp_arg, old != cmp_arg,
                                       old < cmp_arg, old <= cmp_arg,
                                       old > cmp_arg, old >= cmp_arg)):
            first, second = C.c_uint32(0), C.c_uint32(old)
            ready = threading.Event()
            waited = []

            def wait():
                deadline = Timespec(0, 200_000_000)
                ready.set()
                waited.append(futex(second, private, old, C.byref(deadline), first, 0))

            worker = threading.Thread(target=wait)
            worker.start()
            ready.wait()
            # OR zero preserves the word, permitting retries until the waiter
            # has entered the kernel. A timeout bounds the false comparisons.
            deadline = time.monotonic() + 0.15
            woke = (0, 0)
            while time.monotonic() < deadline and worker.is_alive():
                woke = futex(first, 5 | private, 0, C.c_long(1), second,
                             encode(2, 0, cmp, cmp_arg))
                if woke != (0, 0):
                    break
                time.sleep(0.001)
            worker.join()
            checks += 1
            want_wait = (0, 0) if expected else (-1, errno.ETIMEDOUT)
            if woke != (int(expected), 0) or waited != [want_wait]:
                failures.append(dict(private=bool(private), old=old, cmp=cmp,
                                     arg=cmp_arg, wake=woke, wait=waited))

print(json.dumps(dict(checks=checks, passed=not failures, failures=failures), indent=2))
raise SystemExit(bool(failures))
