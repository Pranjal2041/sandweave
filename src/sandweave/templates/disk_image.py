"""Read a pinned QCOW2 filesystem without mounting it or starting a VM."""
import copy
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import tarfile
import tempfile
from urllib.parse import urlsplit
import zipfile


def validate(image):
    allowed = {'url', 'sha256', 'format', 'image_sha256', 'member', 'partition'}
    if set(image) - allowed or image.get('format') not in ('qcow2', 'qcow2.zip'):
        raise ValueError('disk images require format="qcow2" or "qcow2.zip"')
    url = urlsplit(image.get('url', ''))
    if url.scheme != 'https' or not url.hostname or url.username or url.password:
        raise ValueError('disk image URL must use HTTPS without embedded credentials')
    for name in ('sha256', 'image_sha256'):
        value = image.get(name)
        if name == 'image_sha256' and image['format'] == 'qcow2' and value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
            raise ValueError('disk image requires a SHA-256 checksum: ' + name)
    if 'partition' in image and (type(image['partition']) is not int or image['partition'] < 1):
        raise ValueError('disk image partition must be a positive integer')
    if 'member' in image and (not isinstance(image['member'], str) or '\0' in image['member']):
        raise ValueError('disk image archive member must be a string')
    return image


def extract(archive, image, output):
    """Select one archive member; never extract archive paths onto the host."""
    with zipfile.ZipFile(archive) as source:
        members = [entry for entry in source.infolist() if
                   (entry.filename == image['member'] if image.get('member') else
                    entry.filename.endswith('.qcow2')) and not entry.is_dir()]
        if len(members) != 1:
            raise ValueError('disk archive must select exactly one QCOW2 member')
        digest = hashlib.sha256()
        with source.open(members[0]) as stream, Path(output).open('xb') as destination:
            while chunk := stream.read(1024 * 1024):
                destination.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != image['image_sha256']:
            Path(output).unlink()
            raise ValueError('disk image checksum mismatch')


def acl(value):
    """Translate ext4's compact on-disk ACL into the Linux xattr ABI."""
    if len(value) < 4 or struct.unpack_from('<I', value)[0] != 1:
        raise ValueError('unsupported ext4 ACL format')
    result, offset = bytearray(struct.pack('<I', 2)), 4
    while offset < len(value):
        tag, permissions = struct.unpack_from('<HH', value, offset)
        offset += 4
        identity = 0xffffffff
        if tag in (2, 8):
            identity, = struct.unpack_from('<I', value, offset)
            offset += 4
        elif tag not in (1, 4, 16, 32):
            raise ValueError('invalid ext4 ACL tag')
        result.extend(struct.pack('<HHI', tag, permissions, identity))
    return bytes(result)


def write_filesystem(filesystem, output, *, progress=None):
    """Preserve bytes, ownership, permissions, links and extended attributes."""
    seen, directories = {}, set()
    with tarfile.open(output, 'w', format=tarfile.PAX_FORMAT) as archive:
        pending = [('.', filesystem.root)]
        while pending:
            name, node = pending.pop()
            if name != '.' and (name.startswith('/') or '..' in name.split('/')):
                raise ValueError('unsafe filesystem path: ' + repr(name))
            raw, mode = node.inode, node.inode.i_mode
            if stat.S_ISSOCK(mode):
                continue  # Like tar, omit stale filesystem socket names.
            info = tarfile.TarInfo(name)
            info.uid = raw.i_uid | raw.i_uid_high << 16
            info.gid = raw.i_gid | raw.i_gid_high << 16
            info.mode = stat.S_IMODE(mode)
            info.mtime = node.mtime_ns // 1_000_000_000
            seconds, nanos = divmod(node.mtime_ns, 1_000_000_000)
            info.pax_headers['mtime'] = f'{seconds}.{nanos:09d}'
            # Linux uses the inode-body header to identify xattrs. Unused
            # bytes after a zero header are slack, not an attribute block.
            # Dissect 3.15 instead tests the entire slack area for nonzeros.
            attributes = node
            if raw.i_extra[:4] == b'\0' * 4:
                attributes = copy.copy(node)
                attributes.inode = copy.copy(raw)
                attributes.inode.i_extra = b''
            for attribute in attributes.xattr:
                value = attribute.value
                if attribute.name in ('system.posix_acl_access', 'system.posix_acl_default'):
                    value = acl(value)
                info.pax_headers['SCHILY.xattr.' + attribute.name] = value.decode('utf-8', 'surrogateescape')
            stream = None
            if stat.S_ISDIR(mode):
                if node.inum in directories:
                    raise ValueError('disk image contains a directory cycle')
                directories.add(node.inum)
                info.type = tarfile.DIRTYPE
                children = sorted((entry for entry in node.iterdir() if entry.filename not in ('.', '..')),
                                  key=lambda entry: entry.filename, reverse=True)
                pending.extend((entry.filename if name == '.' else name + '/' + entry.filename, entry)
                               for entry in children)
            elif stat.S_ISLNK(mode):
                info.type, info.linkname = tarfile.SYMTYPE, node.link
            elif stat.S_ISREG(mode):
                if node.inum in seen:
                    info.type, info.linkname = tarfile.LNKTYPE, seen[node.inum]
                else:
                    seen[node.inum] = name
                    info.size, stream = node.size, node.open()
            elif stat.S_ISFIFO(mode):
                info.type = tarfile.FIFOTYPE
            elif stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
                info.type = tarfile.CHRTYPE if stat.S_ISCHR(mode) else tarfile.BLKTYPE
                old, new = struct.unpack_from('<II', raw.i_block)
                if old:
                    info.devmajor, info.devminor = old >> 8, old & 255
                else:
                    info.devmajor = new >> 8 & 4095
                    info.devminor = (new & 255) | ((new >> 12) & 0xfff00)
            else:
                raise ValueError('unsupported disk inode type: ' + name)
            try:
                archive.addfile(info, stream)
            finally:
                if stream is not None:
                    stream.close()
            if progress is not None:
                progress.update(advance=1, detail=name)


def export(source, output, *, partition=None, progress=None):
    from importlib.metadata import PackageNotFoundError, version
    dependencies = {'dissect.extfs': '3.15', 'dissect.hypervisor': '3.21', 'dissect.volume': '3.18'}
    missing = []
    for name, required in dependencies.items():
        try:
            installed = version(name)
        except PackageNotFoundError:
            installed = None
        if installed != required:
            missing.append(name + '==' + required)
    if missing:
        from ..onboarding import install_packages
        install_packages(missing)
    from dissect.hypervisor.disk.qcow2 import QCow2
    from dissect.volume.disk import Disk
    from dissect.extfs import ExtFS
    from dissect.extfs.exceptions import Error as ExtError
    with Path(source).open('rb') as stream:
        # A file handle prevents automatic opening of external backing paths.
        disk = QCow2(stream)
        candidates = []
        for part in Disk(disk.open()).partitions:
            if partition is not None and part.number != partition:
                continue
            try:
                fs = ExtFS(part.open())
                fs.get('/etc/os-release')
                candidates.append((part, fs))
            except ExtError:
                continue
        if len(candidates) != 1:
            raise ValueError('select exactly one ext4 root partition containing /etc/os-release')
        part, filesystem = candidates[0]
        if filesystem.sb.s_feature_incompat & 4:
            raise ValueError('disk filesystem needs journal recovery; provide a cleanly shut down image')
        write_filesystem(filesystem, output, progress=progress)
        return {'partition': part.number, 'filesystem_uuid': str(filesystem.uuid),
                'virtual_size': disk.size}


def prepare(image, root, *, pool=None):
    from .images import PYTHON_NAME, PYTHON_URL, PYTHON_SHA256, PRIVATE, control_archive
    from .resolve import fingerprint
    from ..bootstrap import Builder, download
    from ..sandbox.workspace import (home, locked, atomic_json, file_signature,
                                    verified_digest, _immutable)
    from ..setup_progress import Stage
    validate(image)
    root = Path(root)
    shared = home() / 'images'
    directory = shared
    if pool:
        from ..sandbox.retention import validate as validate_pool
        directory = directory / 'pools' / validate_pool(pool)
    directory.mkdir(parents=True, exist_ok=True)
    key = fingerprint({'image': image, 'python': PYTHON_SHA256, 'format': 1})
    destination = directory / ('disk-' + key + '.erofs')
    receipt = destination.with_suffix('.json')
    verified = {}

    def cached(path, metadata):
        import json
        if not path.is_file() or not metadata.is_file():
            return None
        saved = json.loads(metadata.read_text())
        expected = saved.get('sha256')
        if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
            return None
        stamp = file_signature(path)
        if saved.get('signature') == stamp:
            verified[str(path)] = {'sha256': expected, 'signature': dict(zip(
                ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'), stamp))}
        elif verified_digest(path, verified=verified) != expected:
            return None
        return saved

    with locked(destination.with_suffix('.lock')):
        if pool and not destination.exists():
            original = shared / destination.name
            with locked(original.with_suffix('.lock')):
                metadata = original.with_suffix('.json')
                saved = cached(original, metadata)
                if saved is not None:
                    _immutable(original, destination, sha256=saved['sha256'], verified=verified)
                    atomic_json(receipt, {**saved, 'signature': file_signature(destination)})
                    atomic_json(metadata, {**saved, 'signature': file_signature(original)})
        recorded = cached(destination, receipt)
        if recorded is None:
            archive = download(image['url'], directory / 'downloads',
                               image['sha256'] + '.' + image['format'], sha256=image['sha256'])
            working_root = root / 'pool-builds' / pool if pool else root
            working_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.disk-', dir=working_root) as temporary:
                temporary = Path(temporary)
                source = archive
                if image['format'] == 'qcow2.zip':
                    source = temporary / 'image.qcow2'
                    with Stage('Extract disk image'):
                        extract(archive, image, source)
                filesystem = temporary / 'rootfs.tar'
                with Stage('Read disk filesystem', unit='files') as progress:
                    provenance = export(source, filesystem, partition=image.get('partition'), progress=progress)
                python = download(PYTHON_URL, directory / 'downloads', PYTHON_NAME, sha256=PYTHON_SHA256)
                control = temporary / 'control.tar'
                control_archive(python, control)
                with tarfile.open(filesystem, 'a') as output, tarfile.open(control) as inputs:
                    if any(entry.name == PRIVATE or entry.name.startswith(PRIVATE + '/') for entry in output.getmembers()):
                        raise ValueError('disk image uses the reserved /' + PRIVATE + ' directory')
                    for entry in inputs:
                        renamed = copy.copy(entry)
                        renamed.name = PRIVATE + '/' + entry.name
                        if entry.islnk():
                            renamed.linkname = PRIVATE + '/' + entry.linkname
                        output.addfile(renamed, inputs.extractfile(entry) if entry.isfile() else None)
                output = temporary / 'rootfs.erofs'
                Builder(home()).erofs(root, filesystem, output)
                staged = destination.with_suffix('.tmp')
                _immutable(output, staged)
                os.replace(staged, destination)
                recorded = {'sha256': verified_digest(destination, verified=verified), **provenance}
        relative = ('images/pools/' + pool + '/' if pool else 'images/') + destination.name
        _immutable(destination, root / relative, sha256=recorded['sha256'], verified=verified)
        if recorded.get('signature') != file_signature(destination):
            atomic_json(receipt, {**recorded, 'signature': file_signature(destination)})
    return {'reference': image, 'digest': 'sha256:' + recorded['sha256'],
            'platform': {'os': 'linux', 'architecture': 'amd64'}, 'base_image': relative,
            'agent': '/' + PRIVATE + '/python/bin/python3.13',
            'settings': {'user': 'root', 'env': {}, 'workdir': '/', 'entrypoint': [], 'cmd': []},
            'source': {key: recorded[key] for key in ('partition', 'filesystem_uuid', 'virtual_size')}}
