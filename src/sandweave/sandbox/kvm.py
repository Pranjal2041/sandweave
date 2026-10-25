"""Optional gVisor KVM platform.

Sandboxes use gVisor's systrap platform unless they ask for KVM. ``kvm=True``
requires a worker that can open /dev/kvm; ``kvm="auto"`` uses KVM where the
worker can and systrap elsewhere. A restore that does not choose inherits the
platform its snapshot ran on.
"""
import os

from .errors import UnsupportedFeature


def configure(kvm=None, *, runtime='gvisor', saved=None, gpu=False):
    """Validate a request; return the spec entry, or None for the default platform."""
    if kvm is None and saved is not None:
        # Captures record the platform the sandbox actually ran on.
        kvm = True if saved.get('kvm') is True else None
    if kvm is None or kvm is False:
        return None
    if kvm is not True and kvm != 'auto':
        raise ValueError('kvm must be True, False or "auto"')
    if runtime != 'gvisor':
        raise UnsupportedFeature('kvm requires runtime="gvisor"')
    if kvm is True and gpu:
        raise UnsupportedFeature('GPU sandboxes do not support kvm=True; use kvm="auto" to run them without KVM')
    return kvm


def available():
    """Whether this worker can open /dev/kvm."""
    return os.access('/dev/kvm', os.R_OK | os.W_OK)


def platform(kvm, *, gpu=False):
    """The gVisor platform for a spec's kvm entry on this worker."""
    if kvm is True:
        if not available():
            raise UnsupportedFeature('kvm=True needs a readable and writable /dev/kvm on the worker. A snapshot '
                                     'taken on KVM restores with it unless kvm=False or kvm="auto" is passed')
        return 'kvm'
    if kvm == 'auto':
        return 'kvm' if available() and not gpu else 'systrap'
    if kvm is not None and kvm is not False:
        raise ValueError('kvm must be True, False or "auto"')
    return 'systrap'


def check_worker(spec, connection):
    if spec.get('kvm') and not connection.call('ping').get('kvm_platform'):
        raise UnsupportedFeature('kvm requires Sandweave 0.2.48 or newer on the worker and controller')
