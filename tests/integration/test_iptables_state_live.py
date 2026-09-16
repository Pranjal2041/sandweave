"""Legacy state rules allow TCP/UDP replies and reject unlisted new flows."""
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
                import concurrent.futures, socket, subprocess, threading
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
            ''')], timeout=60)
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert 'iptables-legacy 8 TCP' in result.stdout
            assert 'ip6tables-legacy 8 TCP' in result.stdout
    finally:
        reference._connection.close()
