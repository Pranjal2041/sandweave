"""Worker VNC streams, served on the existing RPC listener."""
import asyncio
import hmac
import socket
import threading

from aiohttp import web

from .streams import CHUNK, close_socket, duplex, receive, websocket


class WorkerStreams:
    def __init__(self, worker, token):
        self.worker, self.token = worker, token
        self.loop = asyncio.new_event_loop()
        self.tasks = {}
        self.thread = threading.Thread(target=self.loop.run_forever,
                                       name='sandweave-vnc', daemon=True)
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self._start(), self.loop).result()

    async def _start(self):
        self.server = web.Server(self.handle, handler_cancellation=True,
                                 keepalive_timeout=10, access_log=None)

    def accept(self, connection):
        # Transfer the untouched accepted socket before either HTTP parser has
        # read headers. No second listener, port or forwarding rule is needed.
        connection.setblocking(False)
        future = asyncio.run_coroutine_threadsafe(
            self.loop.connect_accepted_socket(self.server, connection), self.loop)
        try:
            future.result()
        except BaseException:
            future.cancel()
            connection.close()
            raise

    def destination(self, identity, credential):
        if not hmac.compare_digest(credential, self.token) and not self.worker.management.authorize(
                credential, 'vnc_stream', {'identity': identity}):
            raise PermissionError('byte stream access denied')
        # Only opening needs a short lifecycle lock. The byte pump never takes
        # it, and never starts a guest process or writes command output files.
        with self.worker.lock(identity):
            record = self.worker.describe(identity)
            desktop = record['spec']['template'].get('capabilities', {}).get('desktop')
            if (desktop is None or desktop.get('provider', 'desktop') != 'desktop'
                    or desktop.get('backend', 'xvnc') != 'xvnc'):
                raise ValueError('this desktop template does not expose a VNC server')
            runtime = record.get('runtime_status', {})
            if record['state'] != 'ready' or runtime.get('status') != 'running':
                raise ValueError('the sandbox must be running to open VNC')
            port = runtime.get('ports', {}).get('5901')
            if port is None:
                raise ValueError('the sandbox has no VNC endpoint')
            return int(port)

    async def handle(self, request):
        if request.method != 'GET' or request.path != '/vnc':
            return web.Response(status=404)
        identity = request.query.get('identity', '')
        task = asyncio.current_task()
        self.tasks[task] = identity
        stream, writer = websocket(), None
        try:
            port = await asyncio.to_thread(self.destination, identity,
                                          request.headers.get('X-Sandweave-Token', ''))
            reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 10)
            await stream.prepare(request)

            async def upstream():
                while (data := await receive(stream)) is not None:
                    writer.write(data)
                    await writer.drain()

            async def downstream():
                while data := await reader.read(CHUNK):
                    await stream.send_bytes(data)

            await duplex(upstream(), downstream())
        except Exception as error:
            if not stream.prepared:
                # Stable messages only: do not expose credentials or host paths.
                return web.Response(status=403 if isinstance(error, PermissionError) else 409,
                    headers={'X-Sandweave-Error': str(error) if isinstance(error, ValueError)
                             else 'VNC server unavailable'})
            await close_socket(stream, code=1011, message=b'VNC connection failed')
        finally:
            try:
                if writer is not None:
                    # Both directions are finished. Never wait for a paused
                    # guest to drain a socket while tearing down its stream.
                    writer.transport.abort()
                if stream.prepared:
                    await close_socket(stream)
            finally:
                self.tasks.pop(task, None)
        return stream

    def disconnect(self, identity):
        def cancel():
            for task, member in list(self.tasks.items()):
                if member == identity:
                    task.cancel()
        self.loop.call_soon_threadsafe(cancel)

    async def _close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.server.shutdown(2)
        await self.loop.shutdown_default_executor()

    def close(self):
        asyncio.run_coroutine_threadsafe(self._close(), self.loop).result()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()


class StreamServerMixin:
    """Hand GET sockets to async streaming; keep existing RPC handling intact."""
    def process_request_thread(self, request, client_address):
        try:
            if request.recv(1, socket.MSG_PEEK) == b'G':
                self.streams.accept(request)
                return
        except Exception:
            self.shutdown_request(request)
            return
        super().process_request_thread(request, client_address)
