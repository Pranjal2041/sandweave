"""Versioned template controls: the same registration contract for built-ins and plugins.

A provider implements descriptor(config), bind(sandbox, config), and
attach(context, config, cold=False). Its attached object implements call(method,
parameters) and detach(reason). RPC values use the SDK's framed scalar/byte types.
The context supplies guest commands/files and the selected runtime adapter.
"""
from importlib import import_module, metadata
import time
import uuid

from ..sandbox.errors import UnsupportedFeature, SetupError

BUILTINS = {'desktop': 'sandweave.templates.gnome.controls:DesktopProvider',
            'vr': 'sandweave.templates.vr.controls:VRProvider'}


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
        self.runtime = worker.runtime

    def run(self, command=None, *, argv=None, user='root', env=None, cwd='/workspace', timeout=60, check=True):
        process = uuid.uuid4().hex
        self.worker.command_start(self.id, process, command=command, argv=argv, user=user,
                                  env=env, cwd=cwd, timeout=timeout)
        self.worker.process_stdin(self.id, process, close=True)
        while (state := self.worker.process_status(self.id, process))['returncode'] is None:
            time.sleep(.02)
        output = {stream: self.worker.process_output(self.id, process, stream=stream, size=1024**2)
                  for stream in ('stdout', 'stderr')}
        if check and state['returncode']:
            raise SetupError('template command failed: ' + output['stderr'].decode(errors='replace'),
                             sandbox_id=self.id)
        return {**state, **output}

    def file(self, **kwargs):
        return self.worker.file(self.id, **kwargs)


def descriptors(recipe):
    return {name: provider(config.get('provider', name)).descriptor(config)
            for name, config in recipe['capabilities'].items()}
