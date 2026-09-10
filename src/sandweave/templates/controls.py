"""Versioned template controls: the same registration contract for built-ins and plugins.

A provider implements descriptor(config), bind(sandbox, config), and
attach(context, config, cold=False). Its attached object implements call(method,
parameters) and detach(reason). RPC values use the SDK's framed scalar/byte types.
The context supplies guest commands/files and the selected runtime adapter.
"""
from importlib import import_module, metadata
import hashlib
import inspect
from pathlib import Path
import time
import uuid

from ..sandbox.errors import UnsupportedFeature, SetupError

BUILTINS = {'desktop': 'sandweave.templates.gnome.controls:DesktopProvider',
            'vr': 'sandweave.templates.vr.controls:VRProvider',
            'gamepad': 'sandweave.templates.gamepad.controls:GamepadProvider'}


def provider(name):
    entries = list(metadata.entry_points(group='sandweave.controls.v1', name=name))
    if len(entries) > 1 or (entries and name in BUILTINS):
        raise ValueError('duplicate template control provider: ' + name)
    if entries:
        value = entries[0].load()
    elif name in BUILTINS:
        module, attribute = BUILTINS[name].split(':')
        value = getattr(import_module(module), attribute)
    else:
        raise UnsupportedFeature('template control provider is not installed: ' + name)
    if getattr(value, 'api_version', None) != 1:
        raise UnsupportedFeature('unsupported control provider API version: ' + name)
    return value


class Context:
    def __init__(self, worker, identity):
        self.worker, self.id = worker, identity
        self.runtime = worker.runtime.for_identity(identity)
        from ..sandbox.files import Files
        self.files = Files(self)

    def _call(self, operation, **parameters):
        return self.worker.dispatch(operation, {'identity': self.id, **parameters})

    def exec(self, command=None, *, argv=None, **options):
        from ..sandbox.process import Process
        process = uuid.uuid4().hex
        self.worker.command_start(self.id, process, command=command, argv=argv, **options)
        return Process(self, process)

    def run(self, command=None, *, argv=None, user='root', env=None, cwd=None, timeout=60, check=True):
        timeout = self.worker.remaining(self.id, timeout)
        process = uuid.uuid4().hex
        self.worker.command_start(self.id, process, command=command, argv=argv, user=user,
                                  env=env, cwd=cwd, timeout=timeout)
        self.worker.process_stdin(self.id, process, close=True)
        while (state := self.worker.process_status(self.id, process))['returncode'] is None:
            time.sleep(.02)
        output = {stream: self.worker.process_output(self.id, process, stream=stream, size=1024**2)
                  for stream in ('stdout', 'stderr')}
        if check and (state['returncode'] or state.get('output_limited')):
            raise SetupError('template command failed: ' + output['stderr'].decode(errors='replace'),
                             sandbox_id=self.id)
        return {**state, **output}

    def file(self, **kwargs):
        return self.worker.file(self.id, **kwargs)

    def remaining(self, limit=None):
        """Remaining overall startup time; also bounds provider readiness loops."""
        return self.worker.remaining(self.id, limit)


def descriptors(recipe):
    return {name: {**provider(config.get('provider', name)).descriptor(config),
                   'implementation': implementation(config.get('provider', name))}
            for name, config in recipe['capabilities'].items()}


def implementation(name):
    value = provider(name)
    path = inspect.getsourcefile(value)
    entries = list(metadata.entry_points(group='sandweave.controls.v1', name=name))
    distribution = entries[0].dist if entries else None
    return {'provider': name, 'api_version': value.api_version,
            'source_sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest() if path else None,
            'distribution': distribution.metadata['Name'] if distribution else 'sandweave',
            'version': distribution.version if distribution else '0.1.0'}
