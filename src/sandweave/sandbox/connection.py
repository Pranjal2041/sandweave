"""Persistent per-thread control connections with explicit uncertain outcomes."""
import http.client
import socket
import threading

from . import errors
from .wire import decode, encode, MAX_BODY


class Connection:
    def __init__(self, host, port, token, *, timeout=300):
        self.host, self.port, self.token, self.timeout = host, int(port), token, timeout
        self.local = threading.local()
        self.connections, self.lock = [], threading.Lock()

    def call(self, operation, **parameters):
        connection = getattr(self.local, 'connection', None)
        if connection is None:
            connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            self.local.connection = connection
            with self.lock:
                self.connections.append(connection)
        body = encode({'op': operation, 'params': parameters})
        try:
            connection.request('POST', '/rpc', body=body,
                               headers={'X-Sandweave-Token': self.token,
                                        'Content-Type': 'application/vnd.sandweave.frame'})
            response = connection.getresponse()
            if response.status != 200:
                response.read()
                raise errors.SandboxError(f'control request failed: HTTP {response.status}')
            length = int(response.getheader('Content-Length', '-1'))
            if not 0 <= length <= MAX_BODY:
                raise ValueError('invalid response size')
            result = decode(response.read(length))
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            self.local.connection = None
            raise errors.OperationUnknown(f'{operation}: transport failed; delivery outcome unknown: {error}',
                                          operation_id=parameters.get('operation_id') or parameters.get('identity')) from error
        if 'error' in result:
            detail = result['error']
            kind = getattr(errors, detail['kind'], None)
            if kind is not None and isinstance(kind, type) and issubclass(kind, errors.SandboxError):
                raise kind(detail['message'], operation_id=detail.get('operation_id'),
                           sandbox_id=detail.get('sandbox_id'), phase=detail.get('phase'))
            builtin = {'ValueError': ValueError, 'FileNotFoundError': FileNotFoundError,
                       'FileExistsError': FileExistsError, 'PermissionError': PermissionError,
                       'BrokenPipeError': BrokenPipeError, 'TimeoutError': TimeoutError,
                       'KeyError': KeyError}.get(detail['kind'], errors.SandboxError)
            raise builtin(detail['message'])
        return result['result']

    def close(self):
        with self.lock:
            for connection in self.connections:
                connection.close()
            self.connections.clear()
        self.local = threading.local()
