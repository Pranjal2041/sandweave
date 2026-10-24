import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

from aiohttp import web

from sandweave.sandbox import connection as connection_module
from sandweave.sandbox.connection import Connection
from sandweave.sandbox.wire import encode


def test_idle_connections_are_retired_before_reuse(monkeypatch):
    peers = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            peers.append(self.client_address[1])
            payload = encode({'result': 'ok'})
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(connection_module, 'IDLE_REUSE_SECONDS', .2)
    client = Connection('127.0.0.1', server.server_port, 'test')
    try:
        client.call('first')
        client.call('reused')
        time.sleep(.3)
        client.call('retired')
        assert peers[0] == peers[1] != peers[2]
    finally:
        client.close()
        server.shutdown(); server.server_close(); thread.join()


def test_calls_spaced_at_the_server_keepalive_timeout_never_race(monkeypatch):
    """The controller's aiohttp server closes idle connections; scaled to 0.4 s."""
    keepalive = .4
    ready, loop = [], asyncio.new_event_loop()

    async def handle(request):
        await request.read()
        return web.Response(body=encode({'result': 'ok'}), content_type='application/vnd.sandweave.frame')

    async def start():
        app = web.Application()
        app.router.add_post('/rpc', handle)
        runner = web.AppRunner(app, keepalive_timeout=keepalive, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        ready.append((runner, site._server.sockets[0].getsockname()[1]))

    def serve():
        loop.run_until_complete(start())
        loop.run_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    while not ready:
        time.sleep(.01)
    monkeypatch.setattr(connection_module, 'IDLE_REUSE_SECONDS', keepalive / 2)
    client = Connection('127.0.0.1', ready[0][1], 'test')
    try:
        client.call('ping')
        for _ in range(10):
            time.sleep(keepalive)
            assert client.call('ping') == 'ok'
    finally:
        client.close()
        asyncio.run_coroutine_threadsafe(ready[0][0].cleanup(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
