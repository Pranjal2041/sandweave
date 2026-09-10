"""Persistent per-thread control connections with explicit uncertain outcomes."""
import http.client
import socket
import select
import threading
import ssl

from . import errors
from .wire import decode, encode, MAX_BODY


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Connection:
    def __init__(self, host, port, token, *, timeout=300, unix_path=None, tls=False, ca_file=None, rpc_path='/rpc'):
        self.host, self.port, self.token, self.timeout = host, int(port), token, timeout
        self.unix_path = unix_path
        self.tls, self.ca_file, self.rpc_path = tls, ca_file, rpc_path
        self.local = threading.local()
        self.connections, self.lock = [], threading.Lock()

    def clone(self, *, timeout=None):
        return Connection(self.host, self.port, self.token,
                          timeout=self.timeout if timeout is None else timeout, unix_path=self.unix_path,
                          tls=self.tls, ca_file=self.ca_file, rpc_path=self.rpc_path)

    def call(self, operation, **parameters):
        connection = getattr(self.local, 'connection', None)
        if connection is not None and connection.sock is not None:
            # A peer may close an idle keep-alive socket between calls. Detect
            # EOF before sending any bytes; reconnecting here cannot duplicate
            # a mutation. Failures after sending still have uncertain outcomes.
            try:
                readable = select.select([connection.sock], [], [], 0)[0]
                # SSL sockets cannot use MSG_PEEK. With no outstanding request,
                # readable TLS data may be close_notify or a session ticket;
                # replacing the idle connection is safe in either case.
                stale = bool(readable) and (self.tls or connection.sock.recv(1, socket.MSG_PEEK) == b'')
            except OSError:
                stale = True
            if stale:
                connection.close()
                with self.lock:
                    if connection in self.connections:
                        self.connections.remove(connection)
                connection = None
                self.local.connection = None
        if connection is None:
            if self.unix_path:
                connection = UnixHTTPConnection(self.unix_path, self.timeout)
            elif self.tls:
                connection = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                    context=ssl.create_default_context(cafile=self.ca_file))
            else:
                connection = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            self.local.connection = connection
            with self.lock:
                self.connections.append(connection)
        body = encode({'op': operation, 'params': parameters})
        try:
            connection.request('POST', self.rpc_path, body=body,
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
            with self.lock:
                if connection in self.connections:
                    self.connections.remove(connection)
            self.local.connection = None
            raise errors.OperationUnknown(f'{operation}: transport failed; delivery outcome unknown: {error}',
                                          operation_id=parameters.get('operation_id') or parameters.get('identity')) from error
        return self.unwrap(result)

    @staticmethod
    def unwrap(result):
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
            if builtin is errors.SandboxError:
                raise builtin(detail['message'], operation_id=detail.get('operation_id'),
                              sandbox_id=detail.get('sandbox_id'), phase=detail.get('phase'))
            error = builtin(detail['message'])
            for key in ('operation_id', 'sandbox_id', 'phase'):
                setattr(error, key, detail.get(key))
            raise error
        return result['result']

    def close(self):
        with self.lock:
            for connection in self.connections:
                connection.close()
            self.connections.clear()
        self.local = threading.local()
