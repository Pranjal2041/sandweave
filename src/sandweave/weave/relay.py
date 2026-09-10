"""Bounded outbound worker channels. Lost deliveries never replay guest commands."""
from collections import deque
import re
import threading
import time
import uuid

from ..sandbox.connection import Connection
from ..sandbox.errors import OperationUnknown, ResourceUnavailable
from ..sandbox.wire import encode


class Broker:
    def __init__(self):
        self.condition = threading.Condition()
        self.pending, self.queues = {}, {}
        self.bytes = 0
        self.closed = False

    @staticmethod
    def validate(channel):
        if not isinstance(channel, str) or not re.fullmatch('[a-f0-9]{32}', channel):
            raise ValueError('invalid worker channel')

    def call(self, endpoint, operation, parameters, timeout):
        channel = endpoint['relay']
        self.validate(channel)
        identity = uuid.uuid4().hex
        request = dict(id=identity, endpoint=endpoint, method=operation, parameters=parameters)
        size = len(encode(request))
        item = {'request': request, 'done': threading.Event(), 'size': size}
        with self.condition:
            if self.closed:
                raise ResourceUnavailable('controller forwarding is stopping')
            if len(self.pending) >= 256 or self.bytes + size > 128 * 1024**2:
                raise ResourceUnavailable('controller forwarding capacity is in use')
            self.pending[identity] = item
            self.bytes += size
            self.queues.setdefault(channel, deque()).append(identity)
            self.condition.notify_all()
        try:
            if not item['done'].wait(timeout) or 'response' not in item:
                raise OperationUnknown(operation + ': worker reply unavailable; delivery outcome unknown',
                    operation_id=parameters.get('operation_id') or parameters.get('identity'))
            return Connection.unwrap(item['response'])
        finally:
            with self.condition:
                self.pending.pop(identity, None)
                self.bytes -= size
                queue = self.queues.get(channel)
                if queue is not None:
                    try:
                        queue.remove(identity)
                    except ValueError:
                        pass
                    if not queue:
                        self.queues.pop(channel, None)

    def poll(self, channel):
        self.validate(channel)
        deadline = time.monotonic() + 20
        with self.condition:
            while not self.closed:
                queue = self.queues.get(channel)
                while queue:
                    item = self.pending.get(queue.popleft())
                    if item is not None:
                        return item['request']
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(remaining)
        return None

    def result(self, channel, identity, response):
        self.validate(channel)
        with self.condition:
            item = self.pending.get(identity)
            if item is not None and item['request']['endpoint']['relay'] == channel and not item['done'].is_set():
                item['response'] = response
                item['done'].set()
        # Duplicate or late results acknowledge receipt without replaying work.
        return None

    def close(self):
        with self.condition:
            self.closed = True
            for item in self.pending.values():
                item['done'].set()
            self.condition.notify_all()


class RelayConnection:
    def __init__(self, broker, endpoint, *, timeout=300):
        self.broker, self.endpoint, self.timeout = broker, endpoint, timeout
        self.token = endpoint['token']

    def call(self, operation, **parameters):
        return self.broker.call(self.endpoint, operation, parameters, self.timeout)

    def close(self):
        pass
