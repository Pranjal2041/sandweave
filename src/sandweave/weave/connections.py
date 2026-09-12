"""Cancellable worker requests and reusable transports owned by a controller."""
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
import threading

from . import providers
from ..sandbox.errors import ResourceUnavailable, OperationUnknown


def endpoint_key(endpoint):
    # Credentials can be sandbox-scoped or rotated. Workspace distinguishes a
    # replacement worker even if it reuses the old listener's host and port.
    return (endpoint.get('hostname'), endpoint.get('workspace'), endpoint.get('port'),
            endpoint.get('relay'), endpoint.get('ssh_host'), endpoint.get('ssh_port'))


def worker_key(endpoint):
    # A restarted listener keeps the worker's workspace. A replacement worker
    # uses a new workspace, including when it shares the same physical host.
    return (endpoint['hostname'], endpoint['workspace']) if endpoint.get('workspace') else endpoint_key(endpoint)


class Connections:
    def __init__(self, relay):
        self.relay = relay
        self.guard = threading.RLock()
        self.lost = set()
        self.closed = False
        self.transports, self.requests = {}, {}
        self.openers = ThreadPoolExecutor(thread_name_prefix='weave-connect')
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name='weave-io', daemon=True)
        self.thread.start()

    def available(self, endpoint):
        with self.guard:
            return not self.closed and worker_key(endpoint) not in self.lost

    def is_lost(self, endpoint):
        with self.guard:
            return worker_key(endpoint) in self.lost

    def adopt(self, endpoint, connection):
        if isinstance(connection, WorkerConnection):
            return connection
        endpoint = {**endpoint, 'token': connection.token}
        with self.guard:
            if not self.available(endpoint):
                connection.close()
                self.check(endpoint)
            opening = Future()
            opening.set_result(connection)
            previous = self.transports.get(endpoint_key(endpoint))
            self.transports[endpoint_key(endpoint)] = opening
        if previous:
            previous.add_done_callback(self._discard)
        return WorkerConnection(self, endpoint, 300)

    def check(self, endpoint):
        with self.guard:
            if worker_key(endpoint) in self.lost:
                raise ResourceUnavailable('worker was declared lost')
            if self.closed:
                raise OperationUnknown('controller is stopping; worker request outcome is unknown')

    def connection(self, endpoint, *, timeout=300):
        self.check(endpoint)
        if endpoint.get('relay'):
            from .relay import RelayConnection
            return RelayConnection(self.relay, endpoint, timeout=timeout)
        return WorkerConnection(self, endpoint, timeout)

    async def _request(self, endpoint, operation, parameters, timeout, token):
        task = asyncio.current_task()
        with self.guard:
            self.check(endpoint)
            self.requests[task] = worker_key(endpoint)
        deadline = asyncio.timeout(timeout)
        try:
            async with deadline:
                result = await self._forward(endpoint, operation, parameters, token, timeout)
            self.check(endpoint)
            return result
        except TimeoutError as error:
            if deadline.expired():
                raise OperationUnknown('worker request timed out: ' + operation) from error
            raise
        except asyncio.CancelledError:
            self.check(endpoint)
            raise
        finally:
            with self.guard:
                self.requests.pop(task, None)

    def _open(self, endpoint):
        connection = providers.direct(endpoint, timeout=None)
        if not self.available(endpoint):
            connection.close()
            self.check(endpoint)
        return connection

    async def _forward(self, endpoint, operation, parameters, token, timeout):
        if endpoint.get('relay'):
            return await self.relay.acall({**endpoint, 'token': token}, operation, parameters, timeout)
        key = endpoint_key(endpoint)
        with self.guard:
            opening = self.transports.get(key)
            if opening is None:
                # Only connection establishment needs a thread. Forwarded RPCs
                # stay on their caller's event loop and reuse these transports.
                opening = self.transports[key] = self.openers.submit(self._open, endpoint)
        try:
            connection = opening.result() if opening.done() else await asyncio.shield(asyncio.wrap_future(opening))
        except Exception:
            with self.guard:
                if self.transports.get(key) is opening:
                    self.transports.pop(key)
            raise
        self.check(endpoint)
        if hasattr(connection, 'arequest'):
            request = connection.arequest(operation, parameters, token=token)
        else:
            request = asyncio.to_thread(connection.call, operation, **parameters)
        return await request

    def submit(self, endpoint, operation, parameters, timeout, token):
        with self.guard:
            self.check(endpoint)
            return asyncio.run_coroutine_threadsafe(
                self._request(endpoint, operation, parameters, timeout, token), self.loop)

    def exclude(self, endpoints):
        self.relay.exclude(endpoints)
        keys = {worker_key(e) for e in endpoints}
        with self.guard:
            self.lost.update(keys)
            if not self.closed:
                self.loop.call_soon_threadsafe(self._cancel, keys)

    def _cancel(self, keys=None):
        with self.guard:
            requests = list(self.requests.items())
            openings = []
            for key, opening in list(self.transports.items()):
                worker = key[:2] if key[1] else key
                if keys is None or worker in keys:
                    self.transports.pop(key)
                    openings.append(opening)
        for task, key in requests:
            if keys is None or key in keys:
                loop = task.get_loop()
                if not loop.is_closed():
                    loop.call_soon_threadsafe(task.cancel)
        for opening in openings:
            opening.add_done_callback(self._discard)

    @staticmethod
    def _discard(opening):
        if not opening.cancelled() and opening.exception() is None:
            opening.result().close()

    def stop(self):
        with self.guard:
            if not self.closed:
                self.closed = True
                self.loop.call_soon_threadsafe(self._cancel)

    async def _close(self):
        self._cancel()
        with self.guard:
            own = [task for task in self.requests if task.get_loop() is self.loop]
        await asyncio.gather(*own, return_exceptions=True)
        await asyncio.to_thread(self.openers.shutdown, wait=True, cancel_futures=True)
        await self.loop.shutdown_asyncgens()
        await self.loop.shutdown_default_executor()

    def close(self):
        if not self.thread.is_alive():
            return
        self.stop()
        asyncio.run_coroutine_threadsafe(self._close(), self.loop).result()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join()
        self.loop.close()


class WorkerConnection:
    def __init__(self, registry, endpoint, timeout):
        self.registry, self.endpoint, self.timeout = registry, endpoint, timeout
        self.token = endpoint['token']

    def call(self, operation, **parameters):
        return self.registry.submit(self.endpoint, operation, parameters, self.timeout, self.token).result()

    async def acall(self, operation, **parameters):
        return await self.arequest(operation, parameters)

    async def arequest(self, operation, parameters, *, token=None):
        return await self.registry._request(self.endpoint, operation, parameters, self.timeout,
                                            self.token if token is None else token)

    def close(self):
        pass  # The controller owns and reuses the transport.

    async def aclose(self):
        pass
