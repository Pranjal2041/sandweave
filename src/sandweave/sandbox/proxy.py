"""Proxy values and command environment; endpoint enforcement lives outside guests."""
import copy
from dataclasses import dataclass
import ipaddress
import secrets
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ProxyPolicy:
    """Select eligible proxies and distribute assignments within one pool."""
    distribution: str = 'random'
    region: str | None = None

    def __post_init__(self):
        if self.distribution not in ('random', 'round_robin', 'same_proxy', 'same_region'):
            raise ValueError('proxy distribution must be random, round_robin, same_proxy or same_region')
        if self.region is not None and (not isinstance(self.region, str) or not self.region.strip()):
            raise ValueError('proxy region must be a nonempty label')


def catalog(value):
    """Copy input lists; sort region labels so indices survive wire encoding."""
    if not isinstance(value, dict):
        return values(value)
    if not value or any(not isinstance(k, str) or not k.strip() for k in value):
        raise ValueError('proxy regions must be nonempty labels with nonempty URL lists')
    return {region: values(value[region]) for region in sorted(value)}


def entries(value):
    value = catalog(value)
    if isinstance(value, dict):
        return [(region, url) for region, urls in value.items() for url in urls]
    return [(None, url) for url in value]


def candidates(value, policy):
    available = entries(value)
    if (policy.region is not None or policy.distribution == 'same_region') and not isinstance(value, dict):
        raise ValueError('region policies require proxy URLs grouped by region')
    indices = [i for i, (region, _) in enumerate(available) if policy.region is None or region == policy.region]
    if not indices:
        raise ValueError('the requested proxy region has no supplied proxies')
    return available, indices


def select(network, state=None, *, advance=True):
    """Return an index and new state; the caller owns locking and persistence.

    No state means one standalone sandbox. Builders can establish a shared
    selection without consuming a member's rotation position.
    """
    policy = ProxyPolicy(**(network.get('policy') or {}))
    available, indices = candidates(network['proxy'], policy)
    standalone = state is None
    updated = dict(state or {})
    if policy.distribution == 'same_region':
        regions = sorted({available[i][0] for i in indices})
        if 'region' not in updated:
            updated['region'] = regions[secrets.randbelow(len(regions))]
        indices = [i for i in indices if available[i][0] == updated['region']]
    if not indices:
        raise ValueError('saved proxy region no longer has eligible proxies')
    if policy.distribution == 'same_proxy' and not standalone:
        if 'index' not in updated:
            updated['index'] = indices[secrets.randbelow(len(indices))]
        index = updated['index']
    elif policy.distribution in ('round_robin', 'same_region') and not standalone:
        cursor = updated.get('cursor', 0)
        index = indices[cursor % len(indices)]
        if advance:
            updated['cursor'] = cursor + 1
    else:
        index = indices[secrets.randbelow(len(indices))]
    if index not in indices:
        raise ValueError('saved proxy selection no longer matches the policy')
    return {'index': index}, updated


def requires_policy(network):
    return bool(network.get('policy')) or isinstance(network.get('proxy'), dict)


def parse(value):
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https', 'socks5h') or not parsed.hostname
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError
        port = parsed.port if parsed.port is not None else {'http': 80, 'https': 443, 'socks5h': 1080}[parsed.scheme]
        if not 1 <= port <= 65535:
            raise ValueError
        host = parsed.hostname.encode('idna').decode('ascii')
        if ':' in host or not all(c.isalnum() or c in '.-' for c in host):
            raise ValueError
        return parsed, host, port
    except (ValueError, UnicodeError):
        raise ValueError('proxy must be an http://, https:// or socks5h:// URL with an IPv4 address or hostname') from None


def values(value):
    if isinstance(value, str):
        result = (value,)
    elif isinstance(value, (tuple, list)) and value:
        result = tuple(value)
    else:
        raise ValueError('proxy must be a URL or a nonempty list of URLs')
    for url in result:
        parse(url)
    return result


def public(value):
    parsed, host, port = parse(value)
    return f'{parsed.scheme}://{host}:{port}'


def public_resources(resources):
    result = copy.deepcopy(resources)
    network = result.get('network', {})
    if network.get('proxy') is not None:
        value = catalog(network['proxy'])
        network['proxy'] = ({region: [public(url) for url in urls] for region, urls in value.items()}
                            if isinstance(value, dict) else [public(url) for url in value])
    return result


def environment(value):
    # Loopback services remain local. No proxy credentials appear in a command
    # line; compatible tools read their normal proxy environment variables.
    return {**{key: value for key in ('http_proxy', 'https_proxy', 'all_proxy',
                                     'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY')},
            'no_proxy': 'localhost,127.0.0.1,::1', 'NO_PROXY': 'localhost,127.0.0.1,::1'}


def hosts_file(content, hostname, addresses):
    try:
        ipaddress.IPv4Address(hostname)
        return content
    except ValueError:
        pass
    marker = '# sandweave proxy endpoint'
    lines = []
    for line in content.decode(errors='replace').splitlines():
        if marker in line:
            continue
        fields = line.split('#', 1)[0].split()
        if len(fields) > 1 and hostname in fields[1:]:
            aliases = [name for name in fields[1:] if name != hostname]
            if aliases:
                lines.append(fields[0] + ' ' + ' '.join(aliases))
        else:
            lines.append(line)
    lines.extend(f'{address} {hostname} {marker}' for address in addresses)
    return ('\n'.join(lines) + '\n').encode()
