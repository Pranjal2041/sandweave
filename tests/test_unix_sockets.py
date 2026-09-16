"""Real socket I/O under deep and multibyte paths, without moving storage."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from sandweave import _unix_sockets as unix


@pytest.fixture(params=['short', 'deep', 'unicode'])
def directory(tmp_path, request):
    path = tmp_path / {'short': 's', 'deep': 'd' * 180, 'unicode': '文' * 60}[request.param]
    path.mkdir()
    return path


def test_stream_connections_are_concurrent_and_keep_socket_in_storage(directory):
    before = Path.cwd()
    def exchange(index):
        path = directory / f'{index}.sock'
        with socket.socket(socket.AF_UNIX) as listener:
            unix.bind(listener, path)
            listener.listen()
            assert path.is_socket()
            with socket.socket(socket.AF_UNIX) as client:
                unix.connect(client, path)
                with listener.accept()[0] as server:
                    client.sendall(b'hello')
                    assert server.recv(5) == b'hello'
        path.unlink()
    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(exchange, range(64)))
    assert Path.cwd() == before
    assert not list(directory.iterdir())


def test_external_helper_can_bind_descriptor_address(directory):
    path = directory / 'external.sock'
    with unix.Address(path) as address:
        subprocess.run([sys.executable, '-c',
            'import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])',
            address], check=True)
    assert path.is_socket()
    path.unlink()


def test_descriptors_close_after_failure(tmp_path):
    path = tmp_path / ('long-' * 35)
    path.mkdir()
    before = len(list(Path('/proc/self/fd').iterdir()))
    for _ in range(50):
        with socket.socket(socket.AF_UNIX) as client:
            with pytest.raises(FileNotFoundError):
                unix.connect(client, path / 'absent.sock')
    assert len(list(Path('/proc/self/fd').iterdir())) == before


def test_sync_and_async_http_use_deep_socket(directory):
    from aiohttp import web
    from sandweave.sandbox.connection import Connection
    from sandweave.sandbox.wire import encode
    async def run():
        app = web.Application()
        async def reply(request):
            await request.read()
            return web.Response(body=encode({'result': 'ready'}))
        app.router.add_post('/rpc', reply)
        runner = web.AppRunner(app)
        await runner.setup()
        path = directory / 'http.sock'
        with unix.Address(path) as address:
            site = web.UnixSite(runner, address)
            await site.start()
        connection = Connection('localhost', 0, 'test', unix_path=str(path))
        try:
            assert await asyncio.to_thread(connection.call, 'ping') == 'ready'
            assert await connection.acall('ping') == 'ready'
            await connection.aclose()
        finally:
            await connection.aclose()
            await runner.cleanup()
            path.unlink(missing_ok=True)
    asyncio.run(run())
