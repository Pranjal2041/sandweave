#!/usr/bin/env python3
"""Hold a CUDA allocation and RAM nonce; verify both on each HTTP request."""
import argparse
import ctypes as C
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import uuid

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', type=int, default=8000)
args = parser.parse_args()

cuda = C.CDLL('libcuda.so.1')
def call(name, types, *args):
    fn = getattr(cuda, name)
    fn.argtypes, fn.restype = types, C.c_int
    code = fn(*args)
    assert code == 0, (name, code)

call('cuInit', [C.c_uint], 0)
context = C.c_void_p()
call('cuCtxCreate_v2', [C.POINTER(C.c_void_p), C.c_uint, C.c_int], C.byref(context), 0, 0)
memory = C.c_uint64()
call('cuMemAlloc_v2', [C.POINTER(C.c_uint64), C.c_size_t], C.byref(memory), 4096)
call('cuMemsetD8_v2', [C.c_uint64, C.c_ubyte, C.c_size_t], memory, 73, 4096)
nonce, count = uuid.uuid4().hex, 0
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global count
        data = (C.c_ubyte * 4096)()
        call('cuMemcpyDtoH_v2', [C.c_void_p, C.c_uint64, C.c_size_t], data, memory, len(data))
        assert bytes(data) == b'I' * 4096
        count += 1
        body = json.dumps({'nonce': nonce, 'count': count, 'pid': os.getpid(), 'address': memory.value, 'value': data[0]}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
print(json.dumps({'ready': True, 'pid': os.getpid(), 'nonce': nonce}), flush=True)
server = HTTPServer(('0.0.0.0', args.port), Handler)
print(json.dumps({'port': server.server_port}), flush=True)
server.serve_forever()
