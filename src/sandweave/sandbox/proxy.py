"""Proxy values and command environment; endpoint enforcement lives outside guests."""
import copy
import ipaddress
from urllib.parse import urlsplit


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
        network['proxy'] = [public(url) for url in values(network['proxy'])]
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
