"""Async HTTP RPC transport shared by the SDK and outbound worker bridge."""
import asyncio
import ssl

import aiohttp
from yarl import URL

from .connection import Connection
from .errors import OperationUnknown, SandboxError
from .wire import encode, decode, MAX_BODY


class AsyncConnection:
    def __init__(self, host, port, token, *, timeout=300, unix_path=None,
                 tls=False, ca_file=None, rpc_path='/rpc'):
        self.host, self.port, self.token = host, int(port), token
        self.timeout, self.rpc_path = timeout, rpc_path
        self.url = URL.build(scheme='https' if tls else 'http', host=host, port=int(port), path=rpc_path)
        self.ssl = ssl.create_default_context(cafile=ca_file) if tls else True
        connector = (aiohttp.UnixConnector(path=unix_path, limit=0) if unix_path else
                     aiohttp.TCPConnector(limit=0, keepalive_timeout=30))
        self.session = aiohttp.ClientSession(connector=connector, trust_env=False,
            timeout=aiohttp.ClientTimeout(total=None), auto_decompress=False)
        # asyncio.run() cancels remaining tasks before closing its loop. Close
        # pooled sockets there too when a synchronous handle outlives that loop.
        self.guard = asyncio.create_task(self._lifetime(self.session))

    @staticmethod
    async def _lifetime(session):
        try:
            await asyncio.Future()
        finally:
            await session.close()

    async def call(self, operation, **parameters):
        return await self.request(operation, parameters)

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
