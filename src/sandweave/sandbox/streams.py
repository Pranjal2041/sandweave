"""Bounded binary streams on the authenticated SDK transport."""
import asyncio
import atexit
import os
import socket
import threading
from contextlib import suppress

from aiohttp import WSMsgType, web

from .asyncio import dualmethod

CHUNK = 64 * 1024


def websocket():
    return web.WebSocketResponse(max_msg_size=CHUNK + 1, compress=False,
                                 timeout=2)


async def close_socket(stream, **options):
    # aiohttp's close timeout covers waiting for a reply, but its initial
    # close-frame write can itself block behind a peer that stopped reading.
    peer = stream.get_extra_info('socket')
    try:
        await asyncio.wait_for(stream.close(**options), 2)
    except TimeoutError:
        if peer is not None:
            with suppress(OSError):
                peer.shutdown(socket.SHUT_RDWR)


async def receive(socket):
    message = await socket.receive()
    if message.type == WSMsgType.BINARY:
        return message.data
    if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
        if socket.close_code not in (None, 1000, 1001):
            raise ConnectionError('byte stream disconnected')
        return None
    if message.type == WSMsgType.ERROR:
        raise ConnectionError('byte stream transport failed') from socket.exception()
    raise ValueError('byte streams accept binary messages only')


async def duplex(first, second):
    """End both directions together, draining writes instead of buffering copies."""
    tasks = [asyncio.create_task(first), asyncio.create_task(second)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def pipe(left, right):
    async def copy(source, destination):
        while (data := await receive(source)) is not None:
            await destination.send_bytes(data)
    await duplex(copy(left, right), copy(right, left))


class Loop:
    """One lazy I/O thread per client process, including synchronous callers."""
    def __init__(self):
        self.pid = os.getpid()
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever,
                                       name='sandweave-streams', daemon=True)
        self.thread.start()

    def submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)


_loop = None
_guard = threading.Lock()


def io_loop():
    global _loop
    with _guard:
        if _loop is None or _loop.pid != os.getpid():
            _loop = Loop()
        return _loop


@atexit.register
def close_io_loop():
    if _loop is None or _loop.pid != os.getpid():
        return
    async def shutdown():
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    try:
        _loop.submit(shutdown()).result(timeout=5)
    except TimeoutError:
        pass
    finally:
        _loop.loop.call_soon_threadsafe(_loop.loop.stop)
        _loop.thread.join(timeout=1)


def _after_fork():
    global _loop, _guard
    _loop, _guard = None, threading.Lock()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


class ByteStream:
    """Full-duplex bytes; read returns up to n bytes, or b'' at EOF.

    Writes send all supplied bytes with backpressure. Both directions may run
    concurrently. Methods also provide .aio(); no guest command is started.
    """
    def __init__(self, socket, loop):
        self.socket, self.loop = socket, loop
        self.buffer = b''
        self.locally_closed = False
        self.reader, self.writer = asyncio.Lock(), asyncio.Lock()

    @classmethod
    def _opening(cls, connection, identity):
        loop = io_loop()
        task, cancelled = [], threading.Event()
        async def opening():
            task.append(asyncio.current_task())
            if cancelled.is_set():
                raise asyncio.CancelledError
            return cls(await connection.open_stream(identity), loop)
        future = loop.submit(opening())
        def cancel():
            cancelled.set()
            # Cancel the source task, retaining its result if it already
            # finished. Cancelling the concurrent Future could discard a
            # successful handshake before its result reaches this thread.
            def discard(result):
                if not result.cancelled() and result.exception() is None:
                    loop.submit(result.result()._close())
            future.add_done_callback(discard)
            loop.loop.call_soon_threadsafe(lambda: task[0].cancel() if task else None)
        return future, cancel

    @classmethod
    async def open(cls, connection, identity):
        future, cancel = cls._opening(connection, identity)
        try:
            return await asyncio.shield(asyncio.wrap_future(future))
        except BaseException:
            cancel()
            raise

    @classmethod
    def connect(cls, connection, identity):
        future, cancel = cls._opening(connection, identity)
        try:
            return future.result()
        except BaseException:
            cancel()
            raise

    @property
    def closed(self):
        return self.locally_closed or self.socket.closed

    def _wait(self, coroutine):
        future = self.loop.submit(coroutine)
        try:
            return future.result()
        except BaseException:
            future.cancel()
            raise

    async def _read(self, n):
        if type(n) is not int or n < 0:
            raise ValueError('read size must be a nonnegative integer')
        if n == 0 or self.locally_closed:
            return b''
        async with self.reader:
            while not self.buffer:
                data = await receive(self.socket)
                if data is None:
                    return b''
                self.buffer = data
            value, self.buffer = self.buffer[:n], self.buffer[n:]
            return value

    @dualmethod
    def read(self, n=CHUNK):
        return self._wait(self._read(n))

    @read.async_impl
    async def _read_async(self, n=CHUNK):
        return await asyncio.wrap_future(self.loop.submit(self._read(n)))

    async def _write(self, data):
        data = memoryview(data).cast('B')
        async with self.writer:
            if self.closed:
                raise BrokenPipeError('byte stream is closed')
            for offset in range(0, len(data), CHUNK):
                await self.socket.send_bytes(data[offset:offset + CHUNK])
        return len(data)

    @dualmethod
    def write(self, data):
        return self._wait(self._write(data))

    @write.async_impl
    async def _write_async(self, data):
        return await asyncio.wrap_future(self.loop.submit(self._write(data)))

    async def _close(self):
        self.locally_closed = True
        self.buffer = b''
        await close_socket(self.socket)

    @dualmethod
    def close(self):
        self._wait(self._close())

    @close.async_impl
    async def _close_async(self):
        await asyncio.wrap_future(self.loop.submit(self._close()))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close.aio()
