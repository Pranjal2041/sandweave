#!/usr/bin/env python3
"""Compare Linux and guest xattr reads against the same immutable fixture."""
import ctypes
import errno
import json
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
result = {}
for name in ('public', 'shared0', 'shared1', 'acl-allow', 'acl-deny', 'link'):
    path = root / name
    result[name] = {'data': path.read_bytes().hex(),
                    'xattrs': {key: os.getxattr(path, key).hex()
                              for key in sorted(os.listxattr(path))}}
    fd = os.open(path, os.O_RDONLY)
    try:
        assert sorted(os.listxattr(fd)) == sorted(os.listxattr(path))
        for key in os.listxattr(fd):
            assert os.getxattr(fd, key) == os.getxattr(path, key)
    finally:
        os.close(fd)
path = os.fsencode(root / 'public')
for size, expected in ((0, 4), (3, -1), (4, 4), (8, 4)):
    buf = ctypes.create_string_buffer(8)
    ctypes.set_errno(0)
    got = libc.getxattr(path, b'user.binary', buf, ctypes.c_size_t(size))
    assert got == expected, (size, got)
    if got == -1:
        assert ctypes.get_errno() == errno.ERANGE
    if size >= 4:
        assert buf.raw[:4] == b'a\x00b\xff'
ctypes.set_errno(0)
assert libc.listxattr(path, ctypes.create_string_buffer(1), ctypes.c_size_t(1)) == -1
assert ctypes.get_errno() == errno.ERANGE
for key, expected in (('user.missing', errno.ENODATA), ('trusted.missing', errno.ENODATA)):
    try:
        os.getxattr(root / 'public', key)
        raise AssertionError(key)
    except OSError as exc:
        assert exc.errno == expected, exc
print(json.dumps(result, sort_keys=True))
