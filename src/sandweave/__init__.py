"""Sandweave's public API. Importing it never starts a worker or probes hardware."""
from .sandbox.resources import CPU, GPU, Memory, Network
from .sandbox.errors import (
    SandboxError, CacheMiss, CacheConflict, IncompatibleSnapshot,
    UnsupportedFeature, ResourceUnavailable, CommandError, CommandTimeout,
    OperationUnknown, SetupError,
)

__version__ = '0.1.0'
__all__ = ['Sandbox', 'Pool', 'Template', 'SnapshotRef', 'CPU', 'GPU', 'Memory',
           'Network', 'Slurm', 'SandboxError', 'CacheMiss', 'CacheConflict',
           'IncompatibleSnapshot', 'UnsupportedFeature', 'ResourceUnavailable',
           'CommandError', 'CommandTimeout', 'OperationUnknown', 'SetupError']


def __getattr__(name):
    modules = {
        'Sandbox': ('.sandbox.sandbox', 'Sandbox'),
        'Pool': ('.sandbox.pool', 'Pool'),
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
