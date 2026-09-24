"""Async HTTP RPC transport shared by the SDK and outbound worker bridge."""
import asyncio
import ssl
import weakref

import aiohttp
from yarl import URL

from .connection import Connection
from .errors import OperationUnknown, SandboxError
from .wire import encode, decode, MAX_BODY
from .._unix_sockets import Address


class AsyncConnection:
    def __init__(self, host, port, token, *, timeout=300, unix_path=None,
                 tls=False, ca_file=None, rpc_path='/rpc'):
        self.host, self.port, self.token = host, int(port), token
        self.timeout, self.rpc_path = timeout, rpc_path
        self.url = URL.build(scheme='https' if tls else 'http', host=host, port=int(port), path=rpc_path)
        self.ssl = ssl.create_default_context(cafile=ca_file) if tls else True
        address = Address(unix_path) if unix_path else None
        try:
            connector = (aiohttp.UnixConnector(path=address.path, limit=0) if address else
                         aiohttp.TCPConnector(limit=0, keepalive_timeout=30))
            self.session = aiohttp.ClientSession(connector=connector, trust_env=False,
                timeout=aiohttp.ClientTimeout(total=None), auto_decompress=False)
        except BaseException:
            if address:
                address.close()
            raise
        # asyncio.run() cancels remaining tasks before closing its loop. Close
        # pooled sockets there too when a synchronous handle outlives that loop.
        self.address = address
        self.streams = weakref.WeakSet()
        self.sessions, self.stream_session = [self.session], None
        self.guard = asyncio.create_task(self._lifetime(self.sessions, address, self.streams))

    @staticmethod
    async def _lifetime(sessions, address, streams):
        from .streams import close_socket
        try:
            await asyncio.Future()
        finally:
            await asyncio.gather(*(close_socket(stream) for stream in list(streams)), return_exceptions=True)
            for session in sessions:
                await session.close()
            if address:
                address.close()

    async def call(self, operation, **parameters):
        return await self.request(operation, parameters)

    async def open_stream(self, identity, *, path='/vnc', token=None):
        from .streams import CHUNK
        from .errors import UnsupportedFeature
        url = self.url.with_path(self.url.path.rsplit('/', 1)[0] + path)
        if self.stream_session is None:
            # Upgrade sockets have their own pool at the same SDK endpoint.
            # A worker hands them to its async pump before parsing HTTP, while
            # established POST sockets remain with the existing RPC server.
            connector = (aiohttp.UnixConnector(path=self.address.path, limit=0) if self.address else
                         aiohttp.TCPConnector(limit=0))
            self.stream_session = aiohttp.ClientSession(connector=connector, trust_env=False,
                timeout=aiohttp.ClientTimeout(total=None), auto_decompress=False)
            self.sessions.append(self.stream_session)
        try:
            async with asyncio.timeout(self.timeout):
                stream = await self.stream_session.ws_connect(url, params={'identity': identity}, ssl=self.ssl,
                    headers={'X-Sandweave-Token': self.token if token is None else token},
                    max_msg_size=CHUNK + 1, compress=0,
                    timeout=aiohttp.ClientWSTimeout(ws_close=2))
                self.streams.add(stream)
                return stream
        except aiohttp.WSServerHandshakeError as error:
            if error.status == 403:
                raise PermissionError('byte stream access denied') from None
            if error.status in (404, 501):
                raise UnsupportedFeature('VNC streaming requires an updated controller and worker') from None
            raise SandboxError(error.headers.get('X-Sandweave-Error',
                f'cannot open byte stream: HTTP {error.status}')) from None
        except (OSError, aiohttp.ClientError) as error:
            raise ConnectionError('could not open byte stream') from error

    async def request(self, operation, parameters, *, token=None):
        body = encode({'op': operation, 'params': parameters})
        try:
            async with self.session.post(self.url, data=body, ssl=self.ssl,
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    headers={'X-Sandweave-Token': self.token if token is None else token,
                             'Content-Type': 'application/vnd.sandweave.frame'}) as response:
                if response.status != 200:
                    raise SandboxError(f'control request failed: HTTP {response.status}')
                length = response.content_length
                if length is None or not 0 <= length <= MAX_BODY:
                    raise ValueError('invalid response size')
                value = decode(await response.content.readexactly(length))
        except (OSError, aiohttp.ClientError, asyncio.IncompleteReadError) as error:
            raise OperationUnknown(f'{operation}: transport failed; delivery outcome unknown: {error}',
                operation_id=parameters.get('operation_id') or parameters.get('identity')) from error
        return Connection.unwrap(value)

    async def close(self):
        self.guard.cancel()
        await asyncio.gather(self.guard, return_exceptions=True)
        await self.session.close()
        if self.address:
            self.address.close()
