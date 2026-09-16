"""Exercise legacy firewall state matching and real ICMP rejection packets."""
import os
import textwrap

import pytest

from sandweave import Sandbox
from sandweave.templates.build import build

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]


def test_stateful_ipv4_ipv6_firewalls(tmp_path):
    (tmp_path / 'Dockerfile').write_text('FROM ubuntu:22.04\n'
        'RUN apt-get update && apt-get install -y --no-install-recommends iptables python3 '
        '&& rm -rf /var/lib/apt/lists/*\n')
    reference = build(tmp_path)
    try:
        with Sandbox(cache=reference, memory='512MiB') as env:
            result = env.run(argv=['python3', '-c', textwrap.dedent('''
                import concurrent.futures, socket, struct, subprocess, threading
                for family, address, command in [(socket.AF_INET, '127.0.0.1', 'iptables-legacy'),
                                                  (socket.AF_INET6, '::1', 'ip6tables-legacy')]:
                    def rule(*args):
                        return subprocess.check_output([command, *args], text=True)
                    tcp = socket.socket(family)
                    tcp.bind((address, 0)); tcp.listen(16); tcp.settimeout(10)
                    udp = socket.socket(family, socket.SOCK_DGRAM)
                    udp.bind((address, 0)); udp.settimeout(10)
                    blocked = socket.socket(family)
                    blocked.bind((address, 0)); blocked.listen(1); blocked.settimeout(.2)
                    failures = []
                    def serve_tcp():
                        try:
                            for _ in range(8):
                                with tcp.accept()[0] as s:
                                    s.settimeout(5)
                                    assert s.recv(64) == b'tcp'
                                    s.sendall(b'tcp-reply')
                        except BaseException as e: failures.append(e)
                    def serve_udp():
                        try:
                            for _ in range(8):
                                data, peer = udp.recvfrom(64)
                                assert data == b'udp'
                                udp.sendto(b'udp-reply', peer)
                        except BaseException as e: failures.append(e)
                    threads = [threading.Thread(target=f) for f in (serve_tcp, serve_udp)]
                    rule('-N', 'SWSTATE')
                    rule('-A', 'SWSTATE', '-m', 'state', '--state', 'ESTABLISHED,RELATED', '-j', 'ACCEPT')
                    for protocol, sock in [('tcp', tcp), ('udp', udp)]:
                        rule('-A', 'SWSTATE', '-p', protocol, '--dport', str(sock.getsockname()[1]),
                             '-m', 'state', '--state', 'NEW', '-j', 'ACCEPT')
                    rule('-A', 'SWSTATE', '-j', 'REJECT')
                    # The SDK control connection uses another interface.
                    rule('-A', 'OUTPUT', '-o', 'lo', '-j', 'SWSTATE')
                    try:
                        for thread in threads: thread.start()
                        def exchange(index):
                            with socket.socket(family) as s:
                                s.settimeout(5); s.connect(tcp.getsockname()); s.sendall(b'tcp')
                                assert s.recv(64) == b'tcp-reply'
                            with socket.socket(family, socket.SOCK_DGRAM) as s:
                                s.settimeout(5); s.sendto(b'udp', udp.getsockname())
                                assert s.recv(64) == b'udp-reply'
                        with concurrent.futures.ThreadPoolExecutor(8) as executor:
                            list(executor.map(exchange, range(8)))
                        with socket.socket(family) as s:
                            s.settimeout(.5)
                            try: s.connect(blocked.getsockname())
                            except OSError: pass
                            else: raise AssertionError('unlisted new flow was accepted')
                        try: blocked.accept()
                        except TimeoutError: pass
                        else: raise AssertionError('blocked listener received a connection')
                        # A new connection needs NEW for its SYN and ESTABLISHED
                        # for the first reply. Prove the reply rule is effective,
                        # independently of netfilter's optional packet counters.
                        rule('-D', 'SWSTATE', '-m', 'state', '--state', 'ESTABLISHED,RELATED', '-j', 'ACCEPT')
                        with socket.socket(family) as s:
                            s.settimeout(.5)
                            try: s.connect(tcp.getsockname())
                            except OSError: pass
                            else: raise AssertionError('reply passed without an ESTABLISHED rule')
                        rule('-I', 'SWSTATE', '1', '-m', 'state', '--state', 'ESTABLISHED,RELATED', '-j', 'ACCEPT')
                        with socket.socket(family) as s:
                            s.settimeout(5); s.connect(tcp.getsockname())
                        print(command, '8 TCP and 8 UDP exchanges passed; unlisted flow rejected', flush=True)
                    finally:
                        rule('-D', 'OUTPUT', '-o', 'lo', '-j', 'SWSTATE')
                        for thread in threads:
                            if thread.ident is not None: thread.join(12)
                        tcp.close(); udp.close(); blocked.close()
                    assert not failures, failures
                    modes = ([('icmp-net-unreachable', 0), ('icmp-host-unreachable', 1),
                              ('icmp-proto-unreachable', 2), ('icmp-port-unreachable', 3),
                              ('icmp-net-prohibited', 9), ('icmp-host-prohibited', 10),
                              ('icmp-admin-prohibited', 13)] if family == socket.AF_INET else
                             [('icmp6-no-route', 0), ('icmp6-adm-prohibited', 1),
                              ('icmp6-addr-unreachable', 3),
                              ('icmp6-port-unreachable', 4), ('icmp6-policy-fail', 5),
                              ('icmp6-reject-route', 6)])
                    # ip6tables does not expose the obsolete NOT_NEIGHBOUR
                    # ABI value; the engine's parser tests cover that value.
                    protocol = socket.IPPROTO_ICMP if family == socket.AF_INET else socket.IPPROTO_ICMPV6
                    for mode, code in modes:
                        with socket.socket(family, socket.SOCK_DGRAM) as sink, \\
                             socket.socket(family, socket.SOCK_DGRAM) as sender, \\
                             socket.socket(family, socket.SOCK_RAW, protocol) as capture:
                            sink.bind((address, 0)); sink.setblocking(False)
                            capture.settimeout(3)
                            port = sink.getsockname()[1]
                            args = ['OUTPUT', '-o', 'lo', '-p', 'udp', '--dport', str(port),
                                    '-j', 'REJECT', '--reject-with', mode]
                            rule('-A', *args)
                            try:
                                sender.sendto(b'reject-probe', sink.getsockname())
                                while True:
                                    packet = capture.recv(4096)
                                    offset = (packet[0] & 15) * 4 if family == socket.AF_INET else 0
                                    icmp = packet[offset:]
                                    quote_size = (icmp[8] & 15) * 4 if family == socket.AF_INET else 40
                                    quoted_port = struct.unpack_from('!H', icmp, 8 + quote_size + 2)[0]
                                    if quoted_port == port:
                                        assert icmp[0] == (3 if family == socket.AF_INET else 1), (mode, icmp[:2])
                                        assert icmp[1] == code, (mode, icmp[:2])
                                        break
                                try: sink.recv(64)
                                except BlockingIOError: pass
                                else: raise AssertionError('rejected payload reached its destination')
                            finally:
                                rule('-D', *args)
                    print(command, len(modes), 'ICMP rejection codes verified on the wire', flush=True)
            ''')], timeout=60)
            assert result.returncode == 0, result.stdout + '\n' + result.stderr
            assert 'iptables-legacy 8 TCP' in result.stdout
            assert 'ip6tables-legacy 8 TCP' in result.stdout
            assert 'iptables-legacy 7 ICMP rejection codes verified on the wire' in result.stdout
            assert 'ip6tables-legacy 6 ICMP rejection codes verified on the wire' in result.stdout
    finally:
        reference._connection.close()
