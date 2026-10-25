#!/usr/bin/env python3
"""Run a template's init as PID 1 inside a Modal sandbox.

Modal starts the sandbox entrypoint below its own init and without
CAP_SYS_ADMIN, so systemd cannot boot directly. A user namespace with an
identity mapping of every outer ID grants namespace-local privileges without
changing file ownership. A new PID namespace makes the template's init PID 1;
a private mount namespace supplies its /proc, /run and systemd cgroup
hierarchy (Modal's gVisor provides cgroup v1 only).

The guest's process identities remain the outer identities. This script uses
only the Python standard library because it runs from the template image.
"""
import argparse
import ctypes
import os
import signal
import sys

CLONE_NEWNS, CLONE_NEWUSER, CLONE_NEWPID = 0x00020000, 0x10000000, 0x20000000
MS_NOSUID, MS_NODEV, MS_NOEXEC, MS_BIND, MS_REC, MS_PRIVATE = 2, 4, 8, 4096, 16384, 1 << 18
libc = ctypes.CDLL(None, use_errno=True)


def check(result, action):
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, f'{action}: {os.strerror(error)}')


def mount(source, target, fstype=None, flags=0, data=None):
    encode = lambda value: value.encode() if value is not None else None
    check(libc.mount(encode(source), encode(target), encode(fstype), ctypes.c_ulong(flags), encode(data)),
          'mount ' + target)


def identity_map(path):
    """Copy the outer mapping, so namespace IDs equal the sandbox's IDs."""
    ranges = []
    for line in open(path):
        inside, _, length = line.split()
        ranges.append(f'{inside} {inside} {length}')
    return '\n'.join(ranges) + '\n'


def prepare(root):
    mount(None, '/', flags=MS_REC | MS_PRIVATE)
    if root != '/':
        mount(root, root, flags=MS_BIND | MS_REC)
        for name in ('dev', 'sys'):
            mount('/' + name, os.path.join(root, name), flags=MS_BIND | MS_REC)
    # A procfs for the new PID namespace, and private runtime directories.
    mount('proc', os.path.join(root, 'proc'), 'proc', MS_NOSUID | MS_NODEV | MS_NOEXEC)
    mount('tmpfs', os.path.join(root, 'run'), 'tmpfs', MS_NOSUID | MS_NODEV, 'mode=755')
    cgroup = os.path.join(root, 'sys/fs/cgroup')
    mount('tmpfs', cgroup, 'tmpfs', MS_NOSUID | MS_NODEV | MS_NOEXEC, 'mode=755')
    os.mkdir(os.path.join(cgroup, 'systemd'))
    mount('cgroup', os.path.join(cgroup, 'systemd'), 'cgroup', MS_NOSUID | MS_NODEV | MS_NOEXEC,
          'none,name=systemd')
    if root != '/':
        os.chroot(root)
    os.chdir('/')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/', help='alternative root directory (diagnostics only)')
    parser.add_argument('--pid-file', help="record the guest init's outer PID here")
    parser.add_argument('init', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.init[1:] if args.init[:1] == ['--'] else args.init
    if not command:
        parser.error('missing init command')
    ready_read, ready_write = os.pipe()
    mapped_read, mapped_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_read); os.close(mapped_write)
        check(libc.unshare(CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWPID), 'unshare')
        os.write(ready_write, b'1'); os.close(ready_write)
        if os.read(mapped_read, 1) != b'1':
            os._exit(111)
        os.close(mapped_read)
        os.setgid(0); os.setuid(0)
        init = os.fork()
        if init == 0:
            prepare(os.path.realpath(args.root))
            os.execv(command[0], command)
        if args.pid_file:
            # This process remains in the outer PID namespace, so init is the
            # guest init's outer PID, as used by nsenter --target.
            with open(args.pid_file + '.tmp', 'w') as stream:
                stream.write(f'{init}\n')
            os.replace(args.pid_file + '.tmp', args.pid_file)
        # Relay termination to the guest init; exit with its status.
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, lambda signum, frame: os.kill(init, signum))
        while True:
            try:
                _, status = os.waitpid(init, 0)
                break
            except InterruptedError:
                continue
        os._exit(os.waitstatus_to_exitcode(status) & 0xff)
    os.close(ready_write); os.close(mapped_read)
    if os.read(ready_read, 1) != b'1':
        raise SystemExit('guest namespace creation failed')
    with open(f'/proc/{child}/uid_map', 'w') as stream:
        stream.write(identity_map('/proc/self/uid_map'))
    with open(f'/proc/{child}/gid_map', 'w') as stream:
        stream.write(identity_map('/proc/self/gid_map'))
    os.write(mapped_write, b'1'); os.close(mapped_write)
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda signum, frame: os.kill(child, signum))
    while True:
        try:
            _, status = os.waitpid(child, 0)
            break
        except InterruptedError:
            continue
    sys.exit(os.waitstatus_to_exitcode(status))


if __name__ == '__main__':
    main()
