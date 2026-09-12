"""Correlated outbound RPCs with asynchronous, per-worker wakeups."""
import asyncio
from collections import OrderedDict, deque
import re
import threading
import time
import uuid

from ..sandbox.connection import Connection
from ..sandbox.errors import OperationUnknown, ResourceUnavailable
from ..sandbox.wire import encode


def wake(future, value=None):
    if not future.done():
        future.set_result(value)


class Broker:
    def __init__(self):
        self.condition = threading.Condition()
        self.pollers, self.async_pollers = {}, {}
        self.pending, self.queues = {}, {}
        self.bytes = 0
        self.closed = False

    @staticmethod
    def validate(channel):
        if not isinstance(channel, str) or not re.fullmatch('[a-f0-9]{32}', channel):
            raise ValueError('invalid worker channel')

    def _begin(self, endpoint, operation, parameters, response=None):
        channel = endpoint['relay']
        self.validate(channel)
        identity = uuid.uuid4().hex
        request = dict(id=identity, endpoint=endpoint, method=operation, parameters=parameters)
        size = len(encode(request))
        item = {'request': request, 'done': threading.Event(), 'size': size, 'async': response}
        with self.condition:
            if self.closed:
                raise ResourceUnavailable('controller forwarding is stopping')
            if self.bytes + size > 128 * 1024**2:
                raise ResourceUnavailable('controller forwarding capacity is in use')
            self.pending[identity] = item
            self.bytes += size
            self.queues.setdefault(channel, OrderedDict())[identity] = item
            waiters = self.async_pollers.get(channel)
            while waiters:
                loop, future = waiters.popleft()
                if not future.done():
                    loop.call_soon_threadsafe(wake, future)
                    break
            if channel in self.pollers:
                self.pollers[channel].notify()
        return identity, item

    def _finish(self, identity, item):
        channel = item['request']['endpoint']['relay']
        with self.condition:
            self.pending.pop(identity, None)
            self.bytes -= item['size']
            queue = self.queues.get(channel)
            if queue is not None:
                queue.pop(identity, None)
                if not queue:
                    self.queues.pop(channel, None)

    @staticmethod
    def _response(item):
        request = item['request']
        if 'response' not in item:
            params = request['parameters']
            raise OperationUnknown(request['method'] + ': worker reply unavailable; delivery outcome unknown',
                operation_id=params.get('operation_id') or params.get('identity'))
        return Connection.unwrap(item['response'])

    def call(self, endpoint, operation, parameters, timeout):
        identity, item = self._begin(endpoint, operation, parameters)
        try:
            item['done'].wait(timeout)
            return self._response(item)
        finally:
            self._finish(identity, item)

    async def acall(self, endpoint, operation, parameters, timeout):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        identity, item = self._begin(endpoint, operation, parameters, (loop, future))
        try:
            try:
                await asyncio.wait_for(future, timeout)
            except TimeoutError:
                pass
            return self._response(item)
        finally:
            self._finish(identity, item)

    def _take(self, channel):
        queue = self.queues.get(channel)
        if queue:
            _, item = queue.popitem(last=False)
            if not queue:
                self.queues.pop(channel, None)
            return item['request']
        return None

    def _take_many(self, channel, limit):
        first = self._take(channel)
        if first is None or limit is None:
            return first
        requests = [first]
        while len(requests) < limit:
            request = self._take(channel)
            if request is None:
                break
            requests.append(request)
        return requests

    @staticmethod
    def _limit(limit):
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 256):
            raise ValueError('relay batch size must be between 1 and 256')

    def poll(self, channel, limit=None):
        self.validate(channel)
        self._limit(limit)
        deadline = time.monotonic() + 20
        with self.condition:
            available = self.pollers.setdefault(channel, threading.Condition(self.condition))
            while not self.closed:
                request = self._take_many(channel, limit)
                if request is not None:
                    return request
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                available.wait(remaining)
        return None

    async def apoll(self, channel, limit=None):
        self.validate(channel)
        self._limit(limit)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while True:
            future = loop.create_future()
            waiter = (loop, future)
            with self.condition:
                if self.closed:
                    return None
                request = self._take_many(channel, limit)
                if request is not None:
                    return request
                self.async_pollers.setdefault(channel, deque()).append(waiter)
            try:
                await asyncio.wait_for(future, max(0, deadline - loop.time()))
            except TimeoutError:
                return None
            finally:
                with self.condition:
                    waiters = self.async_pollers.get(channel)
                    if waiters is not None:
                        try:
                            waiters.remove(waiter)
                        except ValueError:
                            pass
                        if not waiters:
                            self.async_pollers.pop(channel, None)

    def result(self, channel, identity, response):
        self.validate(channel)
        with self.condition:
            item = self.pending.get(identity)
            if item is not None and item['request']['endpoint']['relay'] == channel and not item['done'].is_set():
                item['response'] = response
                item['done'].set()
                if item['async']:
                    loop, future = item['async']
                    loop.call_soon_threadsafe(wake, future)
        return None

    def results(self, channel, responses):
        self.validate(channel)
        if not isinstance(responses, list) or not 1 <= len(responses) <= 64:
            raise ValueError('expected between 1 and 64 worker responses')
        if any(not isinstance(r, dict) or set(r) != {'identity', 'response'} for r in responses):
            raise ValueError('invalid worker response batch')
        for response in responses:
            self.result(channel, **response)

    def close(self):
        with self.condition:
            self.closed = True
            for item in self.pending.values():
                item['done'].set()
                if item['async']:
                    loop, future = item['async']
                    loop.call_soon_threadsafe(wake, future)
            for available in self.pollers.values():
                available.notify_all()
            for waiters in self.async_pollers.values():
                for loop, future in waiters:
                    loop.call_soon_threadsafe(wake, future)
            self.condition.notify_all()


class RelayConnection:
    def __init__(self, broker, endpoint, *, timeout=300):
        self.broker, self.endpoint, self.timeout = broker, endpoint, timeout
        self.token = endpoint['token']

    def call(self, operation, **parameters):
        return self.broker.call(self.endpoint, operation, parameters, self.timeout)

    async def acall(self, operation, **parameters):
        return await self.broker.acall(self.endpoint, operation, parameters, self.timeout)

    def close(self):
        pass
