"""Small extraction adapters for the pinned upstream Apptainer installer.

Only that installer's curl/rpm2cpio/cpio arguments are supported. The adapters
run in its private staging directory; they are not installed as host commands.
"""
import bz2
import gzip
import lzma
import os
from pathlib import Path
import shutil
import stat
import struct
import sys
import tempfile
import urllib.request


def exact(stream, size):
    value = stream.read(size)
    if len(value) != size:
        raise ValueError('Truncated package archive')
    return value


def header(stream):
    value = exact(stream, 16)
    if value[:4] != b'\x8e\xad\xe8\x01':
        raise ValueError('Invalid RPM header')
    entries, data = struct.unpack('>II', value[8:])
    total = entries * 16 + data
    if total > 64 * 1024**2:
        raise ValueError('RPM header is too large')
    exact(stream, total)
    return 16 + total


def rpm2cpio(arguments):
    if len(arguments) != 1:
        raise ValueError('rpm2cpio expects one filename or -')
    stream = sys.stdin.buffer if arguments[0] == '-' else open(arguments[0], 'rb')
    try:
        if exact(stream, 96)[:4] != b'\xed\xab\xee\xdb':
            raise ValueError('Invalid RPM lead')
        size = header(stream)
        exact(stream, (-size) % 8)
        header(stream)
        # Decompressors need a peekable stream; bound RAM using a temporary file.
        with tempfile.TemporaryFile() as compressed:
            shutil.copyfileobj(stream, compressed)
            compressed.seek(0)
            magic = compressed.read(6)
            compressed.seek(0)
            if magic.startswith(b'\x1f\x8b'):
                decoded = gzip.GzipFile(fileobj=compressed)
            elif magic.startswith(b'\xfd7zXZ'):
                decoded = lzma.LZMAFile(compressed)
            elif magic.startswith(b'BZh'):
                decoded = bz2.BZ2File(compressed)
            elif magic.startswith(b'\x28\xb5\x2f\xfd'):
                import zstandard
                decoded = zstandard.ZstdDecompressor().stream_reader(compressed)
            else:
                raise ValueError('Unsupported RPM payload compression')
            with decoded:
                shutil.copyfileobj(decoded, sys.stdout.buffer)
    finally:
        if stream is not sys.stdin.buffer:
            stream.close()


def cpio(arguments):
    if arguments != ['-idum']:
        raise ValueError('Only the installer extraction mode is supported')
    root = Path.cwd().resolve()
    stream = sys.stdin.buffer
    links = {}
    while True:
        record = exact(stream, 110)
        if record[:6] not in (b'070701', b'070702'):
            raise ValueError('Unsupported CPIO archive')
        fields = [int(record[offset:offset + 8], 16) for offset in range(6, 110, 8)]
        inode, mode, uid, gid, nlink, mtime, size, major, minor, rmajor, rminor, name_size, checksum = fields
        if not 1 <= name_size <= 4096:
            raise ValueError('Invalid CPIO path length')
        name = exact(stream, name_size)
        if not name.endswith(b'\0') or b'\0' in name[:-1]:
            raise ValueError('Invalid CPIO pathname')
        name = os.fsdecode(name[:-1])
        exact(stream, (-(110 + name_size)) % 4)
        if name == 'TRAILER!!!':
            while stream.read(1024 * 1024):
                pass
            break
        relative = Path(name)
        if relative == Path('.') and stat.S_ISDIR(mode) and size == 0:
            continue
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('CPIO path escapes the installation')
        path = root / relative
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError('CPIO traverses a link outside the installation')
        path.parent.mkdir(parents=True, exist_ok=True)
        if stat.S_ISREG(mode):
            if path.is_symlink():
                path.unlink()
            # Replacing instead of truncating preserves other hardlinks.
            fd, filename = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as output:
                    remaining = size
                    while remaining:
                        chunk = exact(stream, min(remaining, 1024 * 1024))
                        output.write(chunk)
                        remaining -= len(chunk)
                os.chmod(filename, mode & 0o777)
                os.replace(filename, path)
            finally:
                Path(filename).unlink(missing_ok=True)
            if nlink > 1:
                group = links.setdefault((major, minor, inode), {'paths': [], 'source': None})
                group['paths'].append(path)
                if size:
                    group['source'] = path
        elif stat.S_ISDIR(mode):
            if size:
                raise ValueError('Unexpected CPIO directory data')
            if path.is_symlink():
                raise ValueError('CPIO directory replaces a symlink')
            path.mkdir(exist_ok=True)
            path.chmod((mode & 0o777) | 0o700)
        elif stat.S_ISLNK(mode):
            if size > 4096:
                raise ValueError('CPIO link is too long')
            target = os.fsdecode(exact(stream, size))
            if path.is_symlink() or path.is_file():
                path.unlink()
            path.symlink_to(target)
        else:
            # No device nodes or sockets are needed by unprivileged Apptainer.
            remaining = size
            while remaining:
                amount = min(remaining, 1024 * 1024)
                exact(stream, amount)
                remaining -= amount
        exact(stream, (-size) % 4)
    for group in links.values():
        source = group['source'] or group['paths'][0]
        for path in group['paths']:
            if path != source:
                path.unlink()
                os.link(source, path)


def curl(arguments):
    urls = []
    iterator = iter(arguments)
    for value in iterator:
        if value in ('--connect-timeout', '-Y', '-y'):
            next(iterator)
        elif value in ('-Ls', '-S'):
            continue
        elif value.startswith('https://'):
            urls.append(value)
        else:
            raise ValueError('Unsupported installer curl argument: ' + value)
    if len(urls) != 1:
        raise ValueError('Installer curl expects one HTTPS URL')
    with urllib.request.urlopen(urls[0], timeout=60) as response:
        shutil.copyfileobj(response, sys.stdout.buffer)


def main():
    try:
        functions = {'rpm2cpio': rpm2cpio, 'cpio': cpio, 'curl': curl}
        functions[sys.argv[1]](sys.argv[2:])
    except (OSError, ValueError, EOFError, KeyError, StopIteration) as error:
        print('Apptainer extraction: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
