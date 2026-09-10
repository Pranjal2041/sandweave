"""Authenticated controller service with HTTP, TLS and loopback SSH access."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import ssl

from .controller import Controller
from ..sandbox.ownership import process_identity
from ..sandbox.wire import encode, decode, MAX_BODY
from ..sandbox.workspace import atomic_json


def serve(directory):
    controller = Controller(directory)
    directory = controller.state.root
    settings_file = directory / 'listener.json'
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}
    credential_file = directory / 'credentials.json'
    if credential_file.exists():
        token = json.loads(credential_file.read_text())['token']
    else:
        if settings.get('token_file'):
            from .transport import credential
            token = credential({'token_file': settings['token_file']})
        else:
            token = secrets.token_hex(32)
        atomic_json(credential_file, {'token': token})
    marker = directory / 'controller.json'
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    from .dashboard import Dashboard
    monitoring = directory / 'monitoring.json'
    dashboard = Dashboard(controller, token, **(json.loads(monitoring.read_text()) if monitoring.exists() else {}))
    controller.dashboard = dashboard

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        wbufsize = 64 * 1024

        def setup(self):
            super().setup()
            self.connection.settimeout(60)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def log_message(self, *args):
            pass

        def do_POST(self):
            if dashboard.handle(self):
                return
            if self.path != '/rpc' or not hmac.compare_digest(self.headers.get('X-Sandweave-Token', ''), token):
                self.close_connection = True
                self.send_error(403)
                return
            try:
                size = int(self.headers.get('Content-Length', '-1'))
                if not 8 <= size <= MAX_BODY:
                    raise ValueError('invalid request size')
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError('incomplete request')
                request = decode(body)
                if request['op'] == 'shutdown':
                    result = {'stopping': True}
                    threading.Thread(target=server.shutdown, daemon=True).start()
                else:
                    result = controller.dispatch(request['op'], request.get('params', {}))
                payload = encode({'result': result})
            except Exception as error:
                payload = encode({'error': {'kind': type(error).__name__, 'message': str(error),
                    **{k: getattr(error, k, None) for k in ('operation_id', 'sandbox_id', 'phase')}}})
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.sandweave.frame')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not dashboard.handle(self):
                self.send_error(404)

        do_HEAD = do_GET

    # A new cluster accepts direct HTTP and SSH through the same service.
    # Port zero lets multiple clusters coexist without choosing ports first.
    hostname = settings.get('host', '0.0.0.0')
    server_type = ThreadingHTTPServer
    if ':' in hostname:
        class IPv6HTTPServer(ThreadingHTTPServer):
            address_family = socket.AF_INET6
        server_type = IPv6HTTPServer
    server = server_type((hostname, settings.get('port') or previous.get('port', 0)), Handler)
    server.daemon_threads = True
    local_server = None
    if settings.get('tls_cert'):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(settings['tls_cert'], settings['tls_key'])
        # Handshakes run in request threads under their socket timeout. An
        # incomplete TLS connection must not block the server's accept loop.
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    if settings.get('tls_cert') or hostname not in ('127.0.0.1', 'localhost', '0.0.0.0'):
        local_port = previous.get('local_port', 0)
        if local_port == server.server_port:
            local_port = 0
        local_server = ThreadingHTTPServer(('127.0.0.1', local_port), Handler)
        local_server.daemon_threads = True
        threading.Thread(target=local_server.serve_forever, daemon=True).start()
    controller.start()
    dashboard.monitor.start()
    advertised = socket.gethostname() if hostname in ('0.0.0.0', '::') else hostname
    if ':' in advertised:
        advertised = '[' + advertised + ']'
    atomic_json(marker, {'hostname': socket.gethostname(), 'port': server.server_port, 'token': token,
                        'local_port': local_server.server_port if local_server else server.server_port,
                        'address': ('https' if settings.get('tls_cert') else 'http') + '://' +
                            advertised + ':' + str(server.server_port),
                        'pid': os.getpid(), 'process': process_identity(), 'status': 'ready'})
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        server.server_close()
        if local_server:
            local_server.shutdown()
            local_server.server_close()
        dashboard.monitor.close()
        controller.close()
        atomic_json(marker, {**json.loads(marker.read_text()), 'status': 'stopped'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    serve(args.directory)


if __name__ == '__main__':
    main()
