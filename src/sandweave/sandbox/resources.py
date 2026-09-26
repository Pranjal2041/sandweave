"""Small immutable resource values; guest limits are distinct from host overhead."""
from dataclasses import asdict, dataclass, field
from decimal import Decimal
import math
import re
from pathlib import PurePosixPath
from .proxy import ProxyPolicy


def positive(value, name, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a positive number')
    if not math.isfinite(value) or value <= 0 or (integer and not isinstance(value, int)):
        raise ValueError(f'{name} must be a positive {"integer" if integer else "number"}')
    return value


def memory_bytes(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return positive(value, 'memory', integer=True)
    if not isinstance(value, str):
        raise ValueError('memory must be bytes or a size such as "4GiB"')
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(B|[KMGT]i?B)\s*', value, re.I)
    if not match:
        raise ValueError('invalid memory size: ' + value)
    number, unit = match.groups()
    unit = unit.upper()
    exponent = 0 if unit == 'B' else 'KMGT'.index(unit[0]) + 1
    result = Decimal(number) * ((1024 if 'I' in unit else 1000) ** exponent)
    if result != int(result) or result <= 0:
        raise ValueError('memory must resolve to a positive whole byte count')
    return int(result)


@dataclass(frozen=True)
class CPU:
    vcpus: int = 1
    weight: int = 100
    quota: float | None = None

    def __post_init__(self):
        positive(self.vcpus, 'vcpus', integer=True)
        positive(self.weight, 'weight', integer=True)
        if self.quota is not None:
            positive(self.quota, 'quota')


@dataclass(frozen=True)
class Storage:
    """Writable filesystem backing. Paths are directories on the worker."""
    mode: str = 'disk'
    path: str | None = None

    def __post_init__(self):
        if self.mode not in ('disk', 'memory'):
            raise ValueError('storage mode must be disk or memory')
        if self.path is not None:
            if self.mode != 'disk':
                raise ValueError('storage path requires disk mode')
            if (not isinstance(self.path, str) or not self.path.startswith('/')
                    or any(c in self.path for c in ('\0', '\n', '\r', ':', ','))
                    or '..' in PurePosixPath(self.path).parts):
                raise ValueError('storage path must be an absolute worker directory without .., colons or commas')


@dataclass(frozen=True)
class Memory:
    guest: str | int = '1GiB'
    runtime: str | int = '512MiB'
    disk: str | int | None = None
    disk_path: str | None = None
    reservation: str | int | None = None
    experimental: bool = False

    def __post_init__(self):
        if memory_bytes(self.guest) < 64 * 1024**2:
            raise ValueError('guest memory must be at least 64MiB')
        if memory_bytes(self.runtime) < 32 * 1024**2:
            raise ValueError('runtime memory must be at least 32MiB')
        if type(self.experimental) is not bool:
            raise ValueError('experimental must be a bool')
        if self.reservation is not None:
            if not self.experimental:
                raise ValueError('memory sharing requires experimental=True')
            if not 64 * 1024**2 <= memory_bytes(self.reservation) <= memory_bytes(self.guest):
                raise ValueError('memory reservation must be between 64MiB and guest memory')
        if (self.disk is None) != (self.disk_path is None):
            raise ValueError('disk memory requires both disk and disk_path')
        if self.disk is not None:
            if memory_bytes(self.disk) < 64 * 1024**2:
                raise ValueError('disk memory must be at least 64MiB')
            if (not isinstance(self.disk_path, str) or not self.disk_path.startswith('/')
                    or any(c in self.disk_path for c in ('\0', '\n', '\r', ':', ','))
                    or '..' in PurePosixPath(self.disk_path).parts):
                raise ValueError('disk_path must be an absolute worker directory without .., colons or commas')


@dataclass(frozen=True)
class GPU:
    model: str | tuple[str, ...] | None = None
    sm_chunks: int | None = None
    client_memory: str | int | None = None
    experimental: bool = False

    def __post_init__(self):
        if self.model is not None:
            models = [self.model] if isinstance(self.model, str) else self.model
            if (not isinstance(models, (list, tuple)) or not models or
                    any(not isinstance(model, str) or not model.strip() for model in models)):
                raise ValueError('GPU model must be a nonempty string or sequence of model names')
            if not isinstance(self.model, str):
                object.__setattr__(self, 'model', tuple(models))
        if self.sm_chunks is not None:
            positive(self.sm_chunks, 'sm_chunks', integer=True)
        if self.client_memory is not None:
            memory_bytes(self.client_memory)
        if (self.sm_chunks is not None or self.client_memory is not None) and not self.experimental:
            raise ValueError('MPS sharing requires experimental=True')
        if self.client_memory is not None and self.sm_chunks is None:
            raise ValueError('client_memory requires an MPS sm_chunks partition')


def gpu_matches(request, model):
    selected = request.get('model')
    choices = (selected,) if isinstance(selected, str) else selected
    return not choices or any(choice.lower() in model.lower() for choice in choices)


@dataclass(frozen=True)
class Network:
    mode: str = 'internet'
    allow_cidrs: tuple[str, ...] = ()
    proxy: str | tuple[str, ...] | dict[str, tuple[str, ...]] | None = field(default=None, repr=False)
    policy: ProxyPolicy | None = None
    allowed_hosts: tuple[str, ...] = ()

    def __post_init__(self):
        import ipaddress
        if self.mode not in ('internet', 'offline', 'proxy', 'allowlist'):
            raise ValueError('network mode must be internet, offline, proxy or allowlist')
        if self.allowed_hosts and self.mode != 'allowlist':
            raise ValueError('allowed_hosts requires allowlist mode')
        object.__setattr__(self, 'allowed_hosts', tuple(self.allowed_hosts))
        for host in self.allowed_hosts:
            if not isinstance(host, str) or not host or any(c in host for c in ('\0', '\n', '\r')):
                raise ValueError('invalid network allowlist entry')
            try:
                address = ipaddress.ip_network(host)
            except ValueError:
                name = host[2:] if host.startswith('*.') else host
                if not all(re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?', label)
                           for label in name.rstrip('.').split('.')):
                    raise ValueError('invalid network allowlist hostname: ' + host)
            else:
                if address.version != 4:
                    raise ValueError('only IPv4 network allowlists are currently supported')
        if self.proxy is not None:
            from .proxy import catalog, candidates
            if self.mode in ('offline', 'allowlist') or self.allow_cidrs:
                raise ValueError('proxy networking cannot be combined with offline mode or allow_cidrs')
            policy = self.policy
            if isinstance(policy, dict):
                policy = ProxyPolicy(**policy)
            if policy is not None and not isinstance(policy, ProxyPolicy):
                raise ValueError('policy must be a ProxyPolicy object')
            object.__setattr__(self, 'policy', policy)
            object.__setattr__(self, 'proxy', catalog(self.proxy))
            candidates(self.proxy, policy or ProxyPolicy())
            object.__setattr__(self, 'mode', 'proxy')
        elif self.mode == 'proxy':
            raise ValueError('proxy mode requires a proxy URL')
        elif self.policy is not None:
            raise ValueError('a proxy policy requires proxy URLs')
        object.__setattr__(self, 'allow_cidrs', tuple(self.allow_cidrs))
        for cidr in self.allow_cidrs:
            if ipaddress.ip_network(cidr).version != 4:
                raise ValueError('only IPv4 egress rules are currently supported')
        if self.mode == 'offline' and self.allow_cidrs:
            raise ValueError('offline mode cannot add egress networks')


def normalize(cpu=1, memory='1GiB', gpu=False, network='internet'):
    cpu = cpu if isinstance(cpu, CPU) else CPU(cpu)
    memory = memory if isinstance(memory, Memory) else Memory(memory)
    if gpu is True:
        gpu = GPU()
    elif isinstance(gpu, str):
        gpu = GPU(model=gpu)
    elif gpu is not False and gpu is not None and not isinstance(gpu, GPU):
        raise ValueError('gpu must be a bool, model string or GPU')
    network = network if isinstance(network, Network) else Network(**network) if isinstance(network, dict) else Network(network)
    return {'cpu': asdict(cpu), 'memory': {k: v for k, v in asdict(memory).items()
                                        if v is not None and (k != 'experimental' or v)},
            'gpu': asdict(gpu) if isinstance(gpu, GPU) else None,
            'network': {k: v for k, v in asdict(network).items()
                        if v is not None and (k != 'allowed_hosts' or v or network.mode == 'allowlist')}}


def restore_resources(resources):
    """Placement reservations and disk locations are not captured guest state."""
    import copy
    result = copy.deepcopy(resources)
    result['memory'].pop('disk_path', None)
    result['memory'].pop('reservation', None)
    result['memory'].pop('experimental', None)
    return result


def uses_memory_reservations(spec):
    return (spec['resources']['memory'].get('reservation') is not None or
            any(uses_memory_reservations(service['request']['spec'])
                for service in spec.get('services', {}).values()))
