"""Verify concurrent TCP payloads while slow readers force output backpressure."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import time
import urllib.request

import pytest

from sandweave import Sandbox

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]


def test_concurrent_slow_receivers_preserve_every_byte():
    payload = os.urandom(16 * 1024**2 + 317)
    expected = hashlib.sha256(payload).hexdigest()
    # Exercise host-socket -> passt -> guest TCP with uploads through an explicit
    # localhost forward. Guest access to host addresses remains forbidden.
    code = '''from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hashlib,json,pathlib,time
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        remaining=int(self.headers['Content-Length'])
        total=remaining
        digest=hashlib.sha256()
        time.sleep(.03)
        while remaining:
            data=self.rfile.read(min(4096,remaining))
            if not data: raise RuntimeError('short upload')
            digest.update(data)
            remaining-=len(data)
            if remaining % 131072 < 4096: time.sleep(.002)
        body=json.dumps([total,digest.hexdigest()]).encode()
        self.send_response(200)
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)
server=ThreadingHTTPServer(('0.0.0.0',8080),Handler)
pathlib.Path('/tmp/transfer-ready').touch()
server.serve_forever()
'''
    with Sandbox(cpu=2, memory='1GiB') as env:
        process = env.exec(argv=['python', '-u', '-c', code])
        try:
            for _ in range(100):
                if env.run('test -f /tmp/transfer-ready').returncode == 0:
                    break
                time.sleep(.02)
            else:
                pytest.fail('guest receiver did not start')
            port = env.status()['runtime_status']['ports']['8080']
            def transfer(_):
                request = urllib.request.Request(f'http://127.0.0.1:{port}/', data=payload, method='POST')
                with urllib.request.urlopen(request, timeout=90) as response:
                    return json.load(response)
            with ThreadPoolExecutor(4) as executor:
                assert list(executor.map(transfer, range(16))) == [[len(payload), expected]] * 16
            assert env.run('printf responsive', check=True).stdout == 'responsive'
        finally:
            process.terminate()
