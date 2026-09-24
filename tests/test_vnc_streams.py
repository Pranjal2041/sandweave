"""Real binary sockets through worker, controller and outbound-only relay."""
import asyncio
from contextlib import contextmanager
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import subprocess
import threading
import time

import pytest

from sandweave import Cluster, Sandbox
from sandweave.sandbox.connection import Connection
from sandweave.sandbox.errors import SandboxError
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.streams import ByteStream
from sandweave.sandbox.targets import Endpoint
from sandweave.sandbox.vnc import StreamServerMixin, WorkerStreams
from sandweave.sandbox.wire import encode, decode
from sandweave.weave import providers
from sandweave.weave.worker import Bridge
from test_weave import Executor

BANNER = b'RFB 003.008\n'


class DesktopExecutor(Executor):
    def describe(self, identity):
        record = super().describe(identity)
        record['runtime_status']['ports'] = {'5901': self.vnc_port}
        return record

    def create(self, spec, identity, **options):
        super().create(spec, identity, **options)
        record = self.read(identity)
        record['runtime_status'] = {'status': 'running', 'ports': {'5901': self.vnc_port}}
        self.write(record)
        return self.describe(identity)

    def terminate(self, identity):
        self.streams.disconnect(identity)
        return super().terminate(identity)


@contextmanager
def worker_server(root):
    executor = DesktopExecutor(root, 1)
    executor.streams = streams = WorkerStreams(executor, 'private-worker-token')
    async def echo(reader, writer):
        try:
            if getattr(executor, 'small_buffers', False):
                writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
                writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
            writer.write(BANNER)
            await writer.drain()
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
    async def start():
        return await asyncio.start_server(echo, '127.0.0.1', 0)
    echo_server = asyncio.run_coroutine_threadsafe(start(), streams.loop).result()
    executor.vnc_port = echo_server.sockets[0].getsockname()[1]
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_POST(self):
            value = decode(self.rfile.read(int(self.headers['Content-Length'])))
            credential = self.headers.get('X-Sandweave-Token', '')
            if credential != 'private-worker-token' and not executor.management.authorize(
                    credential, value['op'], value['params']):
                self.send_error(403)
                return
            try:
                result = {'result': executor.call(value['op'], **value['params'])}
            except Exception as error:
                result = {'error': {'kind': type(error).__name__, 'message': str(error)}}
            data = encode(result)
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    class Server(StreamServerMixin, ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = socket.SOMAXCONN
    server = Server(('127.0.0.1', 0), Handler)
    server.streams = streams
    executor.index = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield executor, Endpoint(server.server_port, 'private-worker-token')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        echo_server.close()
        asyncio.run_coroutine_threadsafe(echo_server.wait_closed(), streams.loop).result()
        streams.close()


def exact(stream, count):
    result = bytearray()
    while len(result) < count:
        chunk = stream.read(count - len(result))
        assert chunk, 'unexpected EOF'
        result.extend(chunk)
    return bytes(result)


def eventually(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.01)


def test_local_stream_and_scope(tmp_path):
    with worker_server(tmp_path / 'worker') as (worker, target):
        first = worker.management.apply('sw-first', 'cluster', 1, 'create',
            spec=definition(template='gnome', detached=True)['spec'], operation_id='one')
        worker.management.apply('sw-other', 'cluster', 1, 'create',
            spec=definition(template='gnome', detached=True)['spec'], operation_id='two')
        scoped = Endpoint(target.port, first['token'])
        env = Sandbox.connect('sw-first', target=scoped)
        stream = env.desktop.vnc()
        try:
            assert stream.read(0) == b''
            assert exact(stream, len(BANNER)) == BANNER
            payload = bytes(range(256)) * 4096
            assert stream.write(payload) == len(payload)
            assert exact(stream, len(payload)) == payload
            with pytest.raises(ValueError):
                stream.read(-1)
            with pytest.raises(PermissionError):
                ByteStream.connect(env._connection, 'sw-other')
            # Opening the stream leaves ordinary RPC responsive.
            assert env._call('describe')['id'] == env.id
            stream.close()
            assert stream.read() == b''
            with pytest.raises(BrokenPipeError):
                stream.write(b'late')
        finally:
            stream.close()
            env.close()
        eventually(lambda: not worker.streams.tasks)


def test_async_concurrent_streams_and_cancellation(tmp_path):
    with worker_server(tmp_path / 'worker') as (worker, target):
        worker.create(definition(template='gnome')['spec'], 'sw-many')
        env = Sandbox.connect('sw-many', target=target)
        async def run():
            streams = await asyncio.gather(*(env.desktop.vnc.aio() for _ in range(64)))
            try:
                assert await asyncio.gather(*(s.read.aio() for s in streams)) == [BANNER] * 64
                # Temporary handshake threads exit after socket handoff. Only
                # the ordinary describe RPC may keep a worker HTTP thread.
                eventually(lambda: sum('process_request_thread' in t.name
                                       for t in threading.enumerate()) <= 1)
                idle = asyncio.create_task(streams[0].read.aio())
                await asyncio.sleep(.02)
                idle.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await idle
                async def exchange(index, stream):
                    data = bytes([index]) * 1024
                    assert await stream.write.aio(data) == len(data)
                    assert await stream.read.aio(len(data)) == data
                await asyncio.gather(*(exchange(i, s) for i, s in enumerate(streams)))
                assert (await env._acall('describe'))['id'] == env.id
                # Closing the handle must release streams, including pending reads.
                await env.close.aio()
                await asyncio.sleep(.1)
                assert all(s.closed for s in streams)
            finally:
                await asyncio.gather(*(s.close.aio() for s in streams))
        try:
            asyncio.run(run())
        finally:
            env.close()
        eventually(lambda: not worker.streams.tasks)


@pytest.mark.parametrize('relay,tls', [(False, False), (True, False), (True, True)])
def test_controller_stream_from_connected_handle(tmp_path, monkeypatch, relay, tls):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'client'))
    options, trust = {}, {}
    if tls:
        cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1',
            '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
        options = dict(tls_cert=cert, tls_key=key)
        trust = dict(ca_file=cert)
    cluster = Cluster.start('streams', directory=tmp_path / 'controller', local_worker=False, **options)
    remote = Cluster.connect(cluster.info['connection']['address'], token=cluster.connection.token, **trust)
    bridge = None
    try:
        with worker_server(tmp_path / 'worker') as (worker, target):
            endpoint = dict(hostname=socket.gethostname(), port=target.port, token=target.token)
            if relay:
                endpoint['relay'] = 'f' * 32
                bridge = Bridge(remote.config, endpoint['relay']).start()
            registered = remote.add_worker({'endpoint': endpoint}, slots=2)
            # The client must reach neither worker nor VNC port directly.
            monkeypatch.setattr(providers, 'direct', lambda *a, **k: pytest.fail('direct client connection'))
            with Sandbox(target=remote, template='gnome', cpu=1, detached=True) as source:
                env = Sandbox.connect(source.id, target=remote)
                stream = env.desktop.vnc()
                try:
                    assert exact(stream, 12) == BANNER
                    assert stream.write(b'\x00\xffhello') == 7
                    assert exact(stream, 7) == b'\x00\xffhello'
                    assert env._call('describe')['id'] == source.id
                    # Declared worker loss ends streams as well as RPC waits.
                    remote.remove_worker(registered['id'], lost=True)
                    try:
                        assert stream.read() == b''
                    except ConnectionError:
                        pass
                    with pytest.raises(SandboxError):
                        env.desktop.vnc()
                    # The synthetic worker remains alive; clean it explicitly.
                    worker.terminate(source.id)
                    source._terminated = True
                finally:
                    stream.close()
                    env.close()
            eventually(lambda: not worker.streams.tasks)
    finally:
        if bridge:
            bridge.close()
            for thread in bridge.threads:
                thread.join(5)
        remote.close()
        cluster.stop()
        cluster.close()


def test_non_desktop_cannot_use_reserved_vnc_port(tmp_path):
    with worker_server(tmp_path / 'worker') as (worker, target):
        worker.create(definition()['spec'], 'sw-code')
        connection = Connection('127.0.0.1', target.port, target.token)
        try:
            with pytest.raises(SandboxError, match='does not expose'):
                ByteStream.connect(connection, 'sw-code')
        finally:
            connection.close()


def test_slow_reader_backpressure_does_not_block_another_stream(tmp_path):
    with worker_server(tmp_path / 'worker') as (worker, target):
        worker.small_buffers = True
        accept = worker.streams.accept
        def bounded(connection):
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
            return accept(connection)
        worker.streams.accept = bounded
        worker.create(definition(template='gnome')['spec'], 'sw-pressure')
        env = Sandbox.connect('sw-pressure', target=target)
        async def run():
            slow, fast = await asyncio.gather(env.desktop.vnc.aio(), env.desktop.vnc.aio())
            flood = None
            try:
                assert await slow.read.aio() == await fast.read.aio() == BANNER
                flood = asyncio.create_task(slow.write.aio(b'x' * (32 * 1024**2)))
                await asyncio.sleep(.2)
                assert not flood.done(), 'slow receiver should backpressure the sender'
                for _ in range(10):
                    await asyncio.wait_for(fast.write.aio(b'ping'), 2)
                    assert await asyncio.wait_for(fast.read.aio(4), 2) == b'ping'
                assert (await asyncio.wait_for(env._acall('describe'), 2))['id'] == env.id
            finally:
                if flood:
                    flood.cancel()
                    await asyncio.gather(flood, return_exceptions=True)
                await asyncio.gather(slow.close.aio(), fast.close.aio())
        try:
            asyncio.run(run())
        finally:
            env.close()
        eventually(lambda: not worker.streams.tasks)


def test_cancelled_handshake_releases_worker_request(tmp_path):
    with worker_server(tmp_path / 'worker') as (worker, target):
        worker.create(definition(template='gnome')['spec'], 'sw-opening')
        env = Sandbox.connect('sw-opening', target=target)
        entered, release = threading.Event(), threading.Event()
        destination = worker.streams.destination
        def delayed(*args):
            entered.set()
            assert release.wait(5)
            return destination(*args)
        worker.streams.destination = delayed
        async def run():
            opening = asyncio.create_task(env.desktop.vnc.aio())
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                opening.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await opening
            finally:
                release.set()
        try:
            asyncio.run(run())
            eventually(lambda: not worker.streams.tasks)
        finally:
            release.set()
            env.close()
