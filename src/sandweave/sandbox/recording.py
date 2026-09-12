"""Opt-in desktop recording configuration and retained recording downloads."""
from dataclasses import dataclass, asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .asyncio import dualmethod


@dataclass(frozen=True)
class Recording:
    fps: int = 15
    cursor: bool = True

    def __post_init__(self):
        if type(self.fps) is not int or not 1 <= self.fps <= 60:
            raise ValueError('recording fps must be an integer in 1..60')
        if type(self.cursor) is not bool:
            raise ValueError('recording cursor must be a bool')


def normalize(value):
    if value is False or value is None:
        return None
    if value is True:
        value = Recording()
    if isinstance(value, dict):
        value = Recording(**value)
    if not isinstance(value, Recording):
        raise ValueError('recording must be a bool or Recording configuration')
    return asdict(value)


def validate(spec):
    from .errors import UnsupportedFeature
    options = normalize(spec.get('recording', False))
    if options is not None:
        desktop = spec['template']['capabilities'].get('desktop', {})
        if (spec['runtime'] != 'gvisor' or not desktop or desktop.get('provider', 'desktop') != 'desktop'
                or desktop.get('backend', 'xvnc') != 'xvnc'):
            raise UnsupportedFeature('recording requires a template with Xvnc desktop controls and runtime="gvisor"')
    return options


class RecordingHandle:
    def __init__(self, sandbox):
        self.sandbox = sandbox

    def _call(self, action, **parameters):
        # Retained recordings remain readable after this handle or its pool
        # closes. Cloning preserves the original endpoint, including relays.
        connection = self.sandbox._connection.clone()
        try:
            return connection.call('recording', identity=self.sandbox.id, action=action, **parameters)
        finally:
            connection.close()

    @property
    def info(self):
        return self._call('status')

    @dualmethod
    def stop(self):
        """Finalize recording while leaving the desktop running."""
        return self._call('stop')

    @dualmethod
    def download(self, directory):
        """Finalize and download all segments, timestamps and recovery metadata.

        Existing files are never overwritten. Downloading again into another
        directory is safe, including after sandbox termination.
        """
        destination = Path(directory).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise FileExistsError('recording download requires an empty directory: ' + str(destination))
        self.stop()
        manifest = self._call('manifest')
        for item in manifest['files']:
            relative = Path(item['path'])
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('invalid recording file path')
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.download-', dir=target.parent)
            digest = hashlib.sha256()
            try:
                with os.fdopen(fd, 'wb') as output:
                    offset = 0
                    while offset < item['size']:
                        data = self._call('read', path=item['path'], offset=offset,
                                          size=min(4*1024**2, item['size']-offset))
                        if not data:
                            raise IOError('recording download ended before the advertised size')
                        output.write(data)
                        digest.update(data)
                        offset += len(data)
                    output.flush()
                    os.fsync(output.fileno())
                if digest.hexdigest() != item['sha256']:
                    raise IOError('recording download checksum mismatch: ' + item['path'])
                os.link(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        with (destination / 'recording.json').open('x') as output:
            json.dump(manifest, output, indent=2)
            output.write('\n')
        return destination

    @dualmethod
    def delete(self):
        """Delete this sandbox's finalized recording files from the worker."""
        return self._call('delete')
