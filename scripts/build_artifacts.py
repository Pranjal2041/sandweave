"""Stream guest build outputs to files created by the host installer."""
import hashlib
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile


MAGIC = b'SANDWEAVE-BUILD-1\n'
COMPLETE = b'COMPLETE\n'
FILES = ('rootfs.tar', 'helpers.tar')
CHUNK = 256 * 1024


def exact(stream, size):
    parts = bytearray()
    while len(parts) < size:
        part = stream.read(size - len(parts))
        if not part:
            raise ValueError('Build artifact transfer ended before completion')
        parts.extend(part)
    return bytes(parts)


def send_archive(stream, command):
    """Frame one tar stream; only a successful producer may send its checksum."""
    digest = hashlib.sha256()
    with subprocess.Popen(command, stdout=subprocess.PIPE) as producer:
        try:
            while chunk := producer.stdout.read(CHUNK):
                digest.update(chunk)
                stream.write(struct.pack('!I', len(chunk)))
                stream.write(chunk)
            if producer.wait() != 0:
                raise ValueError('Guest archive creation failed')
        except BaseException:
            producer.terminate()
            raise
    stream.write(struct.pack('!I', 0))
    stream.write(digest.digest())
    stream.flush()


def export(root, helpers, stream):
    """Guest ownership stays in tar headers; no host output directory is mounted."""
    stream.write(MAGIC)
    # --one-file-system includes mountpoint directories without traversing their
    # contents. This retains /proc, /dev, /run, /sys and /tmp without a second,
    # seekable copy of the archive in guest memory.
    send_archive(stream, ['tar', '--numeric-owner', '--xattrs', '--acls',
        '--one-file-system', '--exclude=./sandweave-input',
        '--exclude=./sandweave-output', '-cpf', '-', '-C', str(root), '.'])
    send_archive(stream, ['tar', '-cpf', '-', '-C', str(helpers), '.'])
    stream.write(COMPLETE)
    stream.flush()


def receive(stream, directory):
    """Receive into an unpublished, host-owned directory using bounded memory."""
    directory = Path(directory)
    if exact(stream, len(MAGIC)) != MAGIC:
        raise ValueError('Invalid build artifact stream')
    for name in FILES:
        temporary = directory / (name + '.part')
        digest = hashlib.sha256()
        with temporary.open('xb') as output:
            while True:
                size, = struct.unpack('!I', exact(stream, 4))
                if size == 0:
                    break
                if size > CHUNK:
                    raise ValueError('Invalid build artifact chunk size')
                chunk = exact(stream, size)
                digest.update(chunk)
                output.write(chunk)
            if exact(stream, digest.digest_size) != digest.digest():
                raise ValueError('Build artifact checksum mismatch: ' + name)
            output.flush()
            os.fsync(output.fileno())
        temporary.rename(directory / name)
    if exact(stream, len(COMPLETE)) != COMPLETE or stream.read(1):
        raise ValueError('Invalid build artifact completion record')


def extract_helpers(archive, destination):
    """Extract helpers as the host user, without applying guest UID/GID or ACLs.

    Links are created last and never traversed during extraction. Absolute
    symlinks in upstream packages remain literal links for use inside guests.
    The root filesystem archive itself is never extracted on the host.
    """
    destination = Path(destination).resolve()
    links, seen = [], set()

    def inside(name):
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Build helper path escapes its directory: ' + name)
        if relative != Path('.') and relative.parts[0] not in ('fast-io', 'gpu'):
            raise ValueError('Unexpected build helper path: ' + name)
        path = destination / relative
        if not path.resolve().is_relative_to(destination):
            raise ValueError('Build helper traverses an external link: ' + name)
        return path

    with tarfile.open(archive) as source:
        for member in source:
            path = inside(member.name)
            if path == destination and member.isdir():
                continue
            if path in seen or path.exists() or path.is_symlink():
                # Parent directories may have been created for earlier files.
                if member.isdir() and path.is_dir() and path not in seen:
                    seen.add(path)
                    continue
                raise ValueError('Duplicate build helper path: ' + member.name)
            seen.add(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                path.mkdir()
            elif member.isfile():
                with source.extractfile(member) as input_file, path.open('xb') as output:
                    shutil.copyfileobj(input_file, output)
                    if output.tell() != member.size:
                        raise ValueError('Incomplete build helper: ' + member.name)
                path.chmod(member.mode & 0o777)
            elif member.issym() or member.islnk():
                links.append((path, member))
            else:
                raise ValueError('Unsupported build helper entry: ' + member.name)
        # Hardlinks can refer forward to regular files, but cannot use symlinks.
        for path, member in links:
            if member.islnk():
                target = inside(member.linkname)
                if not target.is_file() or target.is_symlink():
                    raise ValueError('Invalid build helper hardlink: ' + member.name)
                # Copying also works when the host filesystem disallows links.
                shutil.copyfile(target, path, follow_symlinks=False)
                path.chmod(target.stat().st_mode & 0o777)
        for path, member in links:
            if member.issym():
                path.symlink_to(member.linkname)


if __name__ == '__main__':
    export('/', '/sandweave-output', sys.stdout.buffer)
