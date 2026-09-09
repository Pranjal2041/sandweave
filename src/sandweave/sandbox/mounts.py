"""Explicit worker paths; shared external state is never silently rolled back."""
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Mount:
    source: str
    destination: str
    read_only: bool = True
    snapshot: str | None = None


def serialize(mounts):
    result = []
    for mount in mounts or ():
        value = asdict(mount) if isinstance(mount, Mount) else dict(mount)
        if value.get('snapshot') is None:
            value.pop('snapshot', None)
        value['source'], value['destination'] = str(value['source']), str(value['destination'])
        result.append(value)
    return result
