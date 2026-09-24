"""Live byte forwarding, including workers with only outbound connectivity."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import logging
import secrets

from aiohttp import web

from ..sandbox.streams import close_socket, pipe, websocket


class Streams:
    def __init__(self, controller, token):
        self.controller, self.token = controller, token
        self.pending, self.tasks = {}, set()

    @asynccontextmanager
    async def relay(self, endpoint, identity):
        key = secrets.token_hex(32)
        connected, finished = asyncio.get_running_loop().create_future(), asyncio.Event()
        self.pending[key] = (connected, finished)
        # This request carries only the rendezvous. VNC bytes never enter the
        # RPC queue. Keep it pending to propagate worker loss immediately.
        lifetime = asyncio.create_task(self.controller.relay.acall(endpoint, 'vnc_stream',
            {'identity': identity, 'stream': key}, None))
        stream = None
        try:
            done, _ = await asyncio.wait([connected, lifetime], timeout=30,
                                         return_when=asyncio.FIRST_COMPLETED)
            if lifetime in done:
                lifetime.result()
                raise ConnectionError('worker closed the VNC stream before connecting')
            if connected not in done:
                raise TimeoutError('worker did not connect the VNC stream')
            stream = connected.result()
            yield stream, lifetime
        finally:
            self.pending.pop(key, None)
            finished.set()
            try:
                if stream is not None:
                    await close_socket(stream)
            finally:
                lifetime.cancel()
                await asyncio.gather(lifetime, return_exceptions=True)

    async def reverse(self, request):
        item = self.pending.pop(request.query.get('identity', ''), None)
        if item is None:
            return web.Response(status=404)
        connected, finished = item
        stream = websocket()
        await stream.prepare(request)
        if connected.done():
            await close_socket(stream)
            return stream
        connected.set_result(stream)
        try:
            await finished.wait()
        finally:
            await close_socket(stream)
        return stream

    async def handle(self, request):
        if request.method != 'GET' or not hmac.compare_digest(
                request.headers.get('X-Sandweave-Token', ''), self.token):
            return web.Response(status=403)
        task = asyncio.current_task()
        self.tasks.add(task)
        client = None
        try:
            if request.path == '/vnc/relay':
                return await self.reverse(request)
            identity = request.query.get('identity', '')
            endpoint = self.controller.sandbox_endpoint(identity)
            client = websocket()
            if endpoint.get('relay'):
                async with self.relay(endpoint, identity) as (remote, lifetime):
                    await client.prepare(request)
                    forwarding = asyncio.create_task(pipe(client, remote))
                    try:
                        done, _ = await asyncio.wait([forwarding, lifetime],
                                                     return_when=asyncio.FIRST_COMPLETED)
                        for result in done:
                            result.result()
                    finally:
                        forwarding.cancel()
                        await asyncio.gather(forwarding, return_exceptions=True)
            else:
                async with self.controller.connections.stream(endpoint, identity) as remote:
                    await client.prepare(request)
                    await pipe(client, remote)
        except Exception:
            logging.getLogger(__name__).debug('VNC forwarding failed', exc_info=True)
            if client is None or not client.prepared:
                return web.Response(status=409, headers={'X-Sandweave-Error': 'VNC server unavailable'})
            await close_socket(client, code=1011, message=b'VNC connection failed')
        finally:
            try:
                if client is not None and client.prepared:
                    await close_socket(client)
            finally:
                self.tasks.discard(task)
        return client

    async def close(self):
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def bridge(controller, worker, token, parameters):
    remote = await worker.open_stream(parameters['identity'], token=token)
    try:
        upstream = await controller.open_stream(parameters['stream'], path='/vnc/relay')
        try:
            await pipe(remote, upstream)
        finally:
            await close_socket(upstream)
    finally:
        await close_socket(remote)
