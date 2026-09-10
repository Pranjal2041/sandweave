"""Coordination across Sandweave workers; importing this module starts nothing."""

PROTOCOL = 1


def __getattr__(name):
    if name == 'Cluster':
        from .client import Cluster
        return Cluster
    if name == 'Job':
        from .jobs import Job
        return Job
    if name == 'Pool':
        from .pool import Pool
        return Pool
    raise AttributeError(name)
