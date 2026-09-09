"""Guest file access through bounded binary transfers."""
import io
from pathlib import Path
import uuid

from .asyncio import dualmethod

CHUNK = 1024**2


class Files:
    def __init__(self, sandbox):
        self.sandbox = sandbox

    @dualmethod
    def open(self, path, mode='r', *, encoding='utf-8', errors='strict', newline=None):
        """A guest-owned descriptor with normal Python buffering and text decoding."""
        if not isinstance(mode, str) or any(c not in 'rwax+bt' for c in mode) or len(set(mode)) != len(mode):
            raise ValueError('invalid file mode')
        bases = [c for c in 'rwax' if c in mode]
        if len(bases) != 1 or ('b' in mode and 't' in mode):
            raise ValueError('invalid file mode')
        base = bases[0]
        binary_mode = base + ('+' if '+' in mode else '') + 'b'
        stream = RemoteFile(self.sandbox, str(path), binary_mode)
        buffered = (io.BufferedRandom(stream) if '+' in mode else
                    io.BufferedReader(stream) if base == 'r' else io.BufferedWriter(stream))
        return buffered if 'b' in mode else io.TextIOWrapper(buffered, encoding=encoding, errors=errors, newline=newline)

    @dualmethod
    def read_bytes(self, path):
        chunks, offset = [], 0
        while True:
            chunk = self.sandbox._call('file', op='read', path=str(path), offset=offset, size=CHUNK)
            chunks.append(chunk)
            offset += len(chunk)
            if len(chunk) < CHUNK:
                return b''.join(chunks)

    @dualmethod
    def read_text(self, path, encoding='utf-8'):
        return self.read_bytes(path).decode(encoding)

    @dualmethod
    def write_bytes(self, path, data, *, mode=None):
        data = bytes(data)
        for offset in range(0, max(1, len(data)), CHUNK):
            self.sandbox._call('file', op='write', path=str(path), data=data[offset:offset+CHUNK],
                               offset=offset, truncate=(offset == 0), mode=mode)
        return len(data)

    @dualmethod
    def write_text(self, path, text, encoding='utf-8'):
        return self.write_bytes(path, text.encode(encoding))

    @dualmethod
    def upload(self, source, destination):
        source = Path(source)
        if source.is_dir():
            self.sandbox._call('file', op='mkdir', path=str(destination))
            for path in sorted(source.rglob('*')):
                if path.is_symlink():
                    raise ValueError('upload explicit files rather than symlinks')
                if path.is_file():
                    self.upload(path, str(Path(destination) / path.relative_to(source)))
                elif path.is_dir():
                    self.sandbox._call('file', op='mkdir', path=str(Path(destination) / path.relative_to(source)))
            return
        offset = 0
        with source.open('rb') as stream:
            while True:
                chunk = stream.read(CHUNK)
                if not chunk and offset:
                    break
                self.sandbox._call('file', op='write', path=str(destination), data=chunk,
                                   offset=offset, truncate=(offset == 0), mode=source.stat().st_mode & 0o777)
                offset += len(chunk)
                if not chunk:
                    break
        return offset

    @dualmethod
    def download(self, source, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        offset = 0
        with destination.open('wb') as stream:
            while True:
                chunk = self.sandbox._call('file', op='read', path=str(source), offset=offset, size=CHUNK)
                stream.write(chunk)
                offset += len(chunk)
                if len(chunk) < CHUNK:
                    break
        return destination

    @dualmethod
    def stat(self, path):
        return self.sandbox._call('file', op='stat', path=str(path))

    @dualmethod
    def list(self, path):
        return self.sandbox._call('file', op='list', path=str(path))


class RemoteFile(io.RawIOBase):
    def __init__(self, sandbox, path, mode):
        super().__init__()
        self.sandbox, self.name, self.mode = sandbox, path, mode
        self.handle = uuid.uuid4().hex
        self.opened = False
        self._call('open', path=path, mode=mode)
        self.opened = True

    def _call(self, op, **kwargs):
        return self.sandbox._call('file', op=op, handle=self.handle, **kwargs)

    def readable(self):
        return 'r' in self.mode or '+' in self.mode

    def writable(self):
        return self.mode[0] in 'wax' or '+' in self.mode

    def seekable(self):
        return True

    def readinto(self, buffer):
        self._checkClosed()
        data = self._call('read', size=min(len(buffer), CHUNK))
        buffer[:len(data)] = data
        return len(data)

    def write(self, data):
        self._checkClosed()
        return self._call('write', data=bytes(data[:CHUNK]))

    def seek(self, offset, whence=0):
        self._checkClosed()
        return self._call('seek', offset=offset, whence=whence)

    def tell(self):
        self._checkClosed()
        return self._call('tell')

    def truncate(self, size=None):
        self._checkClosed()
        return self._call('truncate', size=self.tell() if size is None else size)

    def close(self):
        if not self.closed:
            try:
                if self.opened:
                    self._call('close')
            finally:
                super().close()
