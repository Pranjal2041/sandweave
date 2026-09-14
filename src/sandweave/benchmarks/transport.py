"""Reach a guest service through the sandbox's existing authenticated route."""
import asyncio
import concurrent.futures
import threading


RELAY = r'''
import os, select, socket, sys
s = socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=15)
s.settimeout(None)
inputs = [s, 0]
while s in inputs:
    for source in select.select(inputs, [], [])[0]:
        if source == 0:
            data = os.read(0, 65536)
            if data:
                s.sendall(data)
            else:
                inputs.remove(0)
                s.shutdown(socket.SHUT_WR)
        else:
            data = s.recv(65536)
            if not data:
                inputs.remove(s)
                break
            view = memoryview(data)
            while view:
                view = view[os.write(1, view):]
s.close()
'''


class GuestPort:
    """Private localhost listener, scoped to one evaluation and its sandbox."""
    def __init__(self, env, port):
        self.env, self.port = env, port
        self.ready = concurrent.futures.Future()
        self.thread = threading.Thread(target=self._thread, name='sandweave-evaluator-route', daemon=True)
        self.connections = set()

    def _thread(self):
        try:
            asyncio.run(self._serve())
        except BaseException as error:
            if not self.ready.done():
                self.ready.set_exception(error)

    async def _serve(self):
        self.loop = asyncio.get_running_loop()
        self.stopped = asyncio.Event()
        async with await asyncio.start_server(self._connection, '127.0.0.1', 0) as server:
            self.ready.set_result(server.sockets[0].getsockname()[1])
            await self.stopped.wait()
            server.close()
            await server.wait_closed()
            connections = list(self.connections)
            for task in connections:
                task.cancel()
            await asyncio.gather(*connections, return_exceptions=True)

    async def _connection(self, reader, writer):
        current = asyncio.current_task()
        self.connections.add(current)
        process, pumps = None, []
        try:
            process = await self.env.exec.aio(argv=['python3', '-u', '-c', RELAY, str(self.port)],
                                               binary=True, timeout=660)

            async def upstream():
                while chunk := await reader.read(65536):
                    await process.stdin.write.aio(chunk)
                await process.stdin.close.aio()

            async def downstream():
                while True:
                    chunk = await process.stdout._achunk()
                    if chunk:
                        writer.write(chunk)
                        await writer.drain()
                    elif await process.poll.aio() is not None:
                        chunk = await process.stdout._achunk()
                        if chunk:
                            writer.write(chunk)
                            await writer.drain()
                        break
                    else:
                        await asyncio.sleep(.01)

            pumps = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            # A client may half-close its request and still expect a response.
            # Keep draining the guest until it closes its side of the stream.
            if pumps[0] in done and pumps[1] not in done:
                await pumps[1]
        except (ConnectionError, OSError):
            pass  # The canonical caller observes a closed socket and raises.
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            try:
                if process is not None:
                    await process.terminate.aio()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
                finally:
                    self.connections.discard(current)

    def __enter__(self):
        self.thread.start()
        return self.ready.result(timeout=30)

    def __exit__(self, *args):
        self.loop.call_soon_threadsafe(self.stopped.set)
        self.thread.join()
