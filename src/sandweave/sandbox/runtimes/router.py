"""Select an execution adapter from each sandbox's persisted specification."""
from pathlib import Path
from importlib import metadata

from ..wire import decode
from ..errors import UnsupportedFeature
from .gvisor.driver import Runtime as GVisor


class Runtime:
    def __init__(self, root):
        self.root = Path(root)
        self.adapters = {'gvisor': GVisor(root)}

    def adapter(self, name):
        if name == 'apptainer' and name not in self.adapters:
            from .apptainer.driver import Runtime as Apptainer
            self.adapters[name] = Apptainer(self.root, self.adapters['gvisor'])
        if name not in self.adapters:
            entries = list(metadata.entry_points(group='sandweave.runtimes.v1', name=name))
            if len(entries) != 1:
                raise UnsupportedFeature('runtime is missing or registered more than once: ' + name)
            provider = entries[0].load()
            if getattr(provider, 'api_version', None) != 1:
                raise UnsupportedFeature('unsupported runtime adapter API version: ' + name)
            self.adapters[name] = provider(self.root)
        return self.adapters[name]

    def for_identity(self, identity):
        record = decode((self.root / 'sandboxes' / (identity + '.bin')).read_bytes())
        return self.adapter(record['spec']['runtime'])

    def create(self, identity, spec, **options):
        return self.adapter(spec['runtime']).create(identity, spec, **options)

    def __getattr__(self, name):
        if name not in ('status', 'agent', 'pause', 'resume', 'capture', 'terminate'):
            raise AttributeError(name)
        return lambda identity, *args, **kwargs: getattr(self.for_identity(identity), name)(identity, *args, **kwargs)
