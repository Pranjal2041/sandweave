"""Linux Unix sockets in arbitrarily deep storage directories.

Only the address passed to bind/connect is shortened. The socket inode stays
in its original directory, with that directory's permissions and lifecycle.
Directory descriptors are private to each caller; no chdir or global lock is
needed. Retain Address for datagram sockets and external helper processes.
"""
import os


class Address:
    def __init__(self, path):
        self.path = os.fsdecode(path)
        self.fd = None
        if len(os.fsencode(self.path)) > 107:
            parent, name = os.path.split(self.path)
            self.fd = os.open(parent or '.', os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
            self.path = f'/proc/{os.getpid()}/fd/{self.fd}/{name}'
            if len(os.fsencode(self.path)) > 107:
                self.close()
                raise ValueError('Unix socket filename is too long: ' + name)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        self.close()


def bind(sock, path):
    with Address(path) as address:
        sock.bind(address)


def connect(sock, path):
    with Address(path) as address:
        sock.connect(address)
