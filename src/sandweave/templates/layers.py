"""Merge OCI layers without extracting guest paths or ownership onto the host."""
import copy
from dataclasses import dataclass
import gzip
import hashlib
from pathlib import Path
import posixpath
import tarfile


def path(value):
    if '\0' in value or value.startswith('/') or '..' in value.split('/'):
        raise ValueError('image archive contains an unsafe path: ' + repr(value))
    return posixpath.normpath(value).removeprefix('./')


def unpack(blob, output, media_type, expected=None):
    """Decompress once; retain byte offsets for the final filesystem stream."""
    with Path(blob).open('rb') as raw, Path(output).open('wb') as destination:
        if media_type.endswith(('+gzip', '.gzip')):
            source = gzip.GzipFile(fileobj=raw)
        elif media_type.endswith('+zstd'):
            import zstandard
            source = zstandard.ZstdDecompressor().stream_reader(raw)
        elif media_type.endswith('.tar'):
            source = raw
        else:
            raise ValueError('unsupported image layer media type: ' + media_type)
        checksum = hashlib.sha256()
        try:
            while data := source.read(1024*1024):
                checksum.update(data)
                destination.write(data)
        finally:
            if source is not raw:
                source.close()
    if expected is not None and 'sha256:' + checksum.hexdigest() != expected:
        raise ValueError('uncompressed image layer digest mismatch')


@dataclass(eq=False)
class Inode:
    info: tarfile.TarInfo
    source: Path


class Filesystem:
    def __init__(self):
        self.entries = {}

    def resolve(self, name, *, follow=False):
        """Resolve parent links within the guest root, including absolute links."""
        parts, resolved, hops = name.split('/'), [], 0
        while parts:
            part = parts.pop(0)
            if part in ('', '.'):
                continue
            if part == '..':
                if resolved:
                    resolved.pop()
                continue
            candidate = '/'.join([*resolved, part])
            inode = self.entries.get(candidate)
            if inode and inode.info.issym() and (parts or follow):
                hops += 1
                if hops > 40:
                    raise ValueError('image contains a symlink loop: ' + name)
                target = inode.info.linkname
                if target.startswith('/'):
                    resolved = []
                parts = target.split('/') + parts
            else:
                resolved.append(part)
        return '/'.join(resolved) or '.'

    def remove(self, name, *, children_only=False):
        for key in list(self.entries):
            child = key != name and (name == '.' or key.startswith(name.rstrip('/') + '/'))
            if (key == name and not children_only) or child:
                del self.entries[key]

    def parents(self, name):
        name = path(name)
        parent = posixpath.dirname(name)
        if parent and parent != '.':
            self.parents(parent)
            previous = self.entries.get(parent)
            if previous and not previous.info.isdir():
                raise ValueError('image path traverses a non-directory: ' + parent)
            if previous is None:
                info = tarfile.TarInfo(parent)
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                self.entries[parent] = Inode(info, None)

    def apply(self, archive, *, prefix=''):
        with tarfile.open(archive, mode='r:') as source:
            members = source.getmembers()
        names = [(member, path(prefix + member.name)) for member in members]
        # Whiteouts affect the previous layers, even if they follow newly
        # written files in the current tar stream.
        for member, name in names:
            base = posixpath.basename(name)
            if base.startswith('.wh.'):
                parent = self.resolve(posixpath.dirname(name) or '.', follow=True)
                if base == '.wh..wh..opq':
                    self.remove(parent, children_only=True)
                else:
                    self.remove(posixpath.join(parent, base[4:]).removeprefix('./'))
        pending = []
        for member, name in names:
            if posixpath.basename(name).startswith('.wh.'):
                continue
            if member.issparse():
                raise ValueError('sparse tar entries are not supported in image layers: ' + name)
            if not (member.isdir() or member.isreg() or member.issym() or member.islnk()
                    or member.ischr() or member.isblk() or member.isfifo()):
                raise ValueError('unsupported image archive entry: ' + name)
            name = self.resolve(name)
            previous = self.entries.get(name)
            if previous and not (previous.info.isdir() and member.isdir()):
                self.remove(name)
            self.parents(name)
            if member.islnk():
                target = self.resolve(path(prefix + member.linkname))
                inode = self.entries.get(target)
                if inode is None:
                    pending.append((name, target))
                    continue
                if not inode.info.isreg():
                    raise ValueError('image hard link must refer to a regular file')
            else:
                inode = Inode(copy.copy(member), Path(archive))
            self.entries[name] = inode
        while pending:
            waiting = []
            for name, target in pending:
                inode = self.entries.get(target)
                if inode is None:
                    waiting.append((name, target))
                elif not inode.info.isreg():
                    raise ValueError('image hard link must refer to a regular file')
                else:
                    self.entries[name] = inode
            if len(waiting) == len(pending):
                raise ValueError('image contains an unresolved hard link')
            pending = waiting

    def write(self, destination):
        emitted = {}
        with tarfile.open(destination, 'w', format=tarfile.PAX_FORMAT) as output:
            for name, inode in sorted(self.entries.items()):
                info = copy.copy(inode.info)
                info.pax_headers = dict(info.pax_headers)
                # Rewritten names/links must not be overridden by original PAX
                # headers. Ownership, timestamps and xattrs remain intact.
                for key in ('path', 'linkpath', 'size'):
                    info.pax_headers.pop(key, None)
                info.name = name
                if info.isreg() and inode in emitted:
                    info.type, info.linkname, info.size = tarfile.LNKTYPE, emitted[inode], 0
                if info.isreg():
                    emitted[inode] = name
                    with inode.source.open('rb') as stream:
                        stream.seek(inode.info.offset_data)
                        output.addfile(info, stream)
                else:
                    output.addfile(info)
