"""Sandweave's public API. Importing it never starts a worker or probes hardware."""
from .sandbox.resources import CPU, GPU, Memory, Network, ProxyPolicy
from .sandbox.mounts import Mount
from .sandbox.errors import (
    SandboxError, CacheMiss, CacheConflict, IncompatibleSnapshot,
    UnsupportedFeature, ResourceUnavailable, CommandError, CommandTimeout,
    OperationUnknown, SetupError, OutputLimitExceeded,
)

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version('sandweave')
except PackageNotFoundError:
    __version__ = '0+unknown'
__all__ = ['Sandbox', 'Pool', 'Cluster', 'Job', 'Template', 'SnapshotRef', 'CPU', 'GPU', 'Memory',
           'Network', 'ProxyPolicy', 'Mount', 'Slurm', 'SandboxError', 'CacheMiss', 'CacheConflict',
           'IncompatibleSnapshot', 'UnsupportedFeature', 'ResourceUnavailable',
           'CommandError', 'CommandTimeout', 'OperationUnknown', 'SetupError', 'OutputLimitExceeded']


def __getattr__(name):
    modules = {
        'Sandbox': ('.sandbox.sandbox', 'Sandbox'),
        'Pool': ('.weave.pool', 'Pool'),
        'Cluster': ('.weave.client', 'Cluster'),
        'Job': ('.weave.jobs', 'Job'),
        'Template': ('.templates', 'Template'),
        'SnapshotRef': ('.sandbox.snapshots', 'SnapshotRef'),
        'Slurm': ('.sandbox.targets', 'Slurm'),
    }
    if name not in modules:
        raise AttributeError(name)
    from importlib import import_module
    module, attribute = modules[name]
    value = getattr(import_module(module, __name__), attribute)
    globals()[name] = value
    return value
