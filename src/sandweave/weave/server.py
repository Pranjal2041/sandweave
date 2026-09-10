"""Private authenticated controller service, reachable remotely through SSH."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import socket
import threading

from .controller import Controller
from ..sandbox.ownership import process_identity
from ..sandbox.wire import encode, decode, MAX_BODY
from ..sandbox.workspace import atomic_json


def serve(directory):
    controller = Controller(directory)
    directory = controller.state.root
    credential_file = directory / 'credentials.json'
    if credential_file.exists():
        token = json.loads(credential_file.read_text())['token']
    else:
        token = secrets.token_hex(32)
        atomic_json(credential_file, {'token': token})
    marker = directory / 'controller.json'
    previous = json.loads(marker.read_text()) if marker.exists() else {}

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
            if self.path != '/rpc' or not hmac.compare_digest(self.headers.get('X-Sandweave-Token', ''), token):
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

    server = ThreadingHTTPServer(('127.0.0.1', previous.get('port', 0)), Handler)
    server.daemon_threads = True
    controller.start()
    atomic_json(marker, {'hostname': socket.gethostname(), 'port': server.server_port, 'token': token,
                        'pid': os.getpid(), 'process': process_identity(), 'status': 'ready'})
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        server.server_close()
        controller.close()
        atomic_json(marker, {**json.loads(marker.read_text()), 'status': 'stopped'})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    serve(args.directory)


if __name__ == '__main__':
    main()
