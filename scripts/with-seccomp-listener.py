#!/usr/bin/env python3
"""Run an acceptance command with an inherited, allow-all seccomp listener.

This reproduces an outer container's notification-listener constraint without
requiring that container runtime or elevated privileges. The command must still
install its own filters normally; it cannot install a second listener.
"""
import ctypes
import os
import platform
import subprocess
import sys


def main():
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise SystemExit('This regression probe requires Linux x86-64')
    command = sys.argv[1:]
    if not command:
        raise SystemExit('usage: with-seccomp-listener.py COMMAND [ARGUMENTS...]')

    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]

    class Program(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ushort), ('filter', ctypes.POINTER(Filter))]

    instruction = Filter(6, 0, 0, 0x7fff0000)  # BPF_RET | BPF_K, SECCOMP_RET_ALLOW.
    program = Program(1, ctypes.pointer(instruction))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0):  # PR_SET_NO_NEW_PRIVS.
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    listener = libc.syscall(317, 1, 8, ctypes.byref(program))  # NEW_LISTENER.
    if listener < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    try:
        # Keep the listener alive in the parent while the child inherits the
        # filter. No request needs servicing because the filter allows all.
        return subprocess.run(command).returncode
    finally:
        os.close(listener)


if __name__ == '__main__':
    raise SystemExit(main())
