"""Async authenticated controller listener for HTTP, TLS and SSH forwarding."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import functools
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
from types import SimpleNamespace

from aiohttp import web

from .controller import Controller
from ..sandbox.ownership import process_identity
from ..sandbox.wire import encode, decode, MAX_BODY
from ..sandbox.workspace import atomic_json


class DashboardRequest:
    """Adapt the existing read-only dashboard handlers without blocking sockets."""
    def __init__(self, request, body):
        self.path, self.command = request.raw_path, request.method
        self.headers, self.rfile, self.wfile = request.headers, io.BytesIO(body), io.BytesIO()
        self.client_address = (request.remote or '', 0)
        self.connection = SimpleNamespace(settimeout=lambda value: None)
        self.secure = request.secure
        self.close_connection = False
        self.status, self.response_headers = 200, {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass


class RPC:
    def __init__(self, controller, token, dashboard, stop):
        self.controller, self.token, self.dashboard, self.stop = controller, token, dashboard, stop
        self.executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix='weave-control')
        self.monitor_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix='weave-dashboard')
        self.connections = {}
        self.connection_lock = asyncio.Lock()

    async def blocking(self, function, *args, monitoring=False, **kwargs):
        executor = self.monitor_executor if monitoring else self.executor
        return await asyncio.get_running_loop().run_in_executor(executor, functools.partial(function, *args, **kwargs))

    async def forward(self, identity, method, parameters):
        # Indexed memory lookup. Neither disk I/O nor lifecycle locks intervene.
        endpoint = self.controller.sandbox_endpoint(identity)
        if endpoint.get('relay'):
            return await self.controller.relay.acall(endpoint, method, parameters, 300)
        key = (endpoint['hostname'], endpoint['port'], endpoint.get('ssh_host'), endpoint.get('ssh_port'))
        connection = self.connections.get(key)
        if connection is None:
            async with self.connection_lock:
                connection = self.connections.get(key)
                if connection is None:
                    connection = await self.blocking(self.controller.connection, endpoint)
                    self.connections[key] = connection
        return await connection.arequest(method, parameters, token=endpoint['token'])

    async def handle(self, request):
        if request.path != '/rpc':
            if request.content_length and request.content_length > 4096:
                return web.Response(status=413)
            body = await asyncio.wait_for(request.read(), 10)
            adapted = DashboardRequest(request, body)
            if not await self.blocking(self.dashboard.handle, adapted, monitoring=True):
                return web.Response(status=404)
            return web.Response(status=adapted.status, headers=adapted.response_headers, body=adapted.wfile.getvalue())
        if request.method != 'POST' or not hmac.compare_digest(request.headers.get('X-Sandweave-Token', ''), self.token):
            return web.Response(status=403)
        try:
            if request.content_length is None or not 8 <= request.content_length <= MAX_BODY:
                raise ValueError('invalid request size')
            message = decode(await asyncio.wait_for(request.read(), 60))
            operation, params = message['op'], message.get('params', {})
            if operation == 'relay_poll':
                result = await self.controller.relay.apoll(**params)
            elif operation == 'relay_result':
                result = self.controller.relay.result(**params)
            elif operation == 'relay_results':
                result = self.controller.relay.results(**params)
            elif operation == 'sandbox_rpc':
                result = await self.forward(**params)
            elif operation == 'shutdown':
                result = {'stopping': True}
                asyncio.get_running_loop().call_later(.05, self.stop.set)
            else:
                result = await self.blocking(self.controller.dispatch, operation, params)
            payload = encode({'result': result})
        except Exception as error:
            payload = encode({'error': {'kind': type(error).__name__, 'message': str(error),
                **{k: getattr(error, k, None) for k in ('operation_id', 'sandbox_id', 'phase')}}})
        return web.Response(body=payload, content_type='application/vnd.sandweave.frame')

    async def close(self):
        await asyncio.to_thread(self.executor.shutdown, wait=True)
        await asyncio.to_thread(self.monitor_executor.shutdown, wait=True)
        for connection in self.connections.values():
            await connection.aclose()
        self.connections.clear()


async def serve_async(directory):
    controller = Controller(directory)
    directory = controller.state.root
    settings_file = directory / 'listener.json'
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    credential_file = directory / 'credentials.json'
    if credential_file.exists():
        token = json.loads(credential_file.read_text())['token']
    else:
        if settings.get('token_file'):
            from .transport import credential
            token = credential({'token_file': settings['token_file']})
        else:
            token = secrets.token_hex(32)
        atomic_json(credential_file, {'token': token})
    marker = directory / 'controller.json'
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    from .dashboard import Dashboard
    monitoring = directory / 'monitoring.json'
    dashboard = Dashboard(controller, token, **(json.loads(monitoring.read_text()) if monitoring.exists() else {}))
    controller.dashboard = dashboard
    stop = asyncio.Event()
    rpc = RPC(controller, token, dashboard, stop)
    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_route('*', '/{path:.*}', rpc.handle)
    runner = web.AppRunner(app, access_log=None, keepalive_timeout=60,
                           handler_cancellation=True, shutdown_timeout=5)
    try:
        await runner.setup()
        hostname = settings.get('host', '0.0.0.0')
        context = None
        if settings.get('tls_cert'):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(settings['tls_cert'], settings['tls_key'])
        site = web.TCPSite(runner, hostname, settings.get('port') or previous.get('port', 0),
                           ssl_context=context, backlog=socket.SOMAXCONN)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        local_port = port
        if context or hostname not in ('127.0.0.1', 'localhost', '0.0.0.0'):
            local_port = previous.get('local_port', 0)
            local = web.TCPSite(runner, '127.0.0.1', 0 if local_port == port else local_port,
                               backlog=socket.SOMAXCONN)
            await local.start()
            local_port = local._server.sockets[0].getsockname()[1]
        controller.start()
        dashboard.monitor.start()
        advertised = socket.gethostname() if hostname in ('0.0.0.0', '::') else hostname
        if ':' in advertised:
            advertised = '[' + advertised + ']'
        atomic_json(marker, {'hostname': socket.gethostname(), 'port': port, 'token': token,
                            'local_port': local_port, 'address': ('https' if context else 'http') + '://' + advertised + ':' + str(port),
                            'pid': os.getpid(), 'process': process_identity(), 'status': 'ready'})
        await stop.wait()
    finally:
        controller.stopping.set()
        controller.relay.close()
        await runner.cleanup()
        await rpc.close()
        await asyncio.to_thread(dashboard.monitor.close)
        await asyncio.to_thread(controller.close)
        if marker.exists():
            atomic_json(marker, {**json.loads(marker.read_text()), 'status': 'stopped'})


def serve(directory):
    asyncio.run(serve_async(directory))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    serve(args.directory)


if __name__ == '__main__':
    main()
