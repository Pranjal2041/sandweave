"""Small immutable resource values; guest limits are distinct from host overhead."""
from dataclasses import asdict, dataclass
from decimal import Decimal
import math
import re


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
class Memory:
    guest: str | int = '1GiB'
    runtime: str | int = '512MiB'

    def __post_init__(self):
        if memory_bytes(self.guest) < 64 * 1024**2:
            raise ValueError('guest memory must be at least 64MiB')
        if memory_bytes(self.runtime) < 32 * 1024**2:
            raise ValueError('runtime memory must be at least 32MiB')


@dataclass(frozen=True)
class GPU:
    model: str | None = None
    sm_chunks: int | None = None
    client_memory: str | int | None = None
    experimental: bool = False

    def __post_init__(self):
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ValueError('GPU model must be a nonempty string')
        if self.sm_chunks is not None:
            positive(self.sm_chunks, 'sm_chunks', integer=True)
        if self.client_memory is not None:
            memory_bytes(self.client_memory)
        if (self.sm_chunks is not None or self.client_memory is not None) and not self.experimental:
            raise ValueError('MPS sharing requires experimental=True')
        if self.client_memory is not None and self.sm_chunks is None:
            raise ValueError('client_memory requires an MPS sm_chunks partition')


@dataclass(frozen=True)
class Network:
    mode: str = 'internet'
    allow_cidrs: tuple[str, ...] = ()

    def __post_init__(self):
        import ipaddress
        if self.mode not in ('internet', 'offline'):
            raise ValueError('network mode must be internet or offline')
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
    network = network if isinstance(network, Network) else Network(network)
    return {'cpu': asdict(cpu), 'memory': asdict(memory),
            'gpu': asdict(gpu) if isinstance(gpu, GPU) else None,
            'network': asdict(network)}
