"""Guest file access through bounded binary transfers."""
import io
from pathlib import Path

from .asyncio import dualmethod

CHUNK = 1024**2


class Files:
    def __init__(self, sandbox):
        self.sandbox = sandbox

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
            for path in sorted(source.rglob('*')):
                if path.is_symlink():
                    raise ValueError('upload explicit files rather than symlinks')
                if path.is_file():
                    self.upload(path, str(Path(destination) / path.relative_to(source)))
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
