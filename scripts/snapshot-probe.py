#!/usr/bin/env python3
"""Guest-only state probe: RAM, deleted open file, abstract Unix and cross-netns TCP."""
import http.server
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time

nonce = secrets.token_hex(16)
counter = 0
lock = threading.Lock()
marker = Path('/opt/snapshot-state')
marker.write_text(nonce)
marker.chmod(0o640)
os.chown(marker, 12345, 6789)
fd = os.open('/tmp/snapshot-unlinked', os.O_CREAT | os.O_RDWR, 0o600)
os.write(fd, ('open-file:' + nonce).encode())
os.lseek(fd, 5, os.SEEK_SET)
os.unlink('/tmp/snapshot-unlinked')
abstract = socket.socket(socket.AF_UNIX)
abstract.bind('\0snapshot-probe-' + nonce)
abstract.listen(1)
unix_client = socket.socket(socket.AF_UNIX)
unix_client.connect('\0snapshot-probe-' + nonce)
unix_server, _ = abstract.accept()
unix_client.sendall(b'queued-before-checkpoint')

commands = [
 ['ip', 'netns', 'add', 'snapshot-net'],
 ['ip', 'link', 'add', 'snap-root', 'type', 'veth', 'peer', 'name', 'snap-peer'],
 ['ip', 'link', 'set', 'snap-peer', 'netns', 'snapshot-net'],
 ['ip', 'addr', 'add', '198.18.0.1/24', 'dev', 'snap-root'],
 ['ip', 'link', 'set', 'snap-root', 'up'],
 ['ip', '-n', 'snapshot-net', 'addr', 'add', '198.18.0.2/24', 'dev', 'snap-peer'],
 ['ip', '-n', 'snapshot-net', 'link', 'set', 'snap-peer', 'up'],
 ['ip', '-n', 'snapshot-net', 'link', 'set', 'lo', 'up'],
]
for command in commands:
    subprocess.run(command, check=True)
server = subprocess.Popen(['ip', 'netns', 'exec', 'snapshot-net', 'python3', '-u', '-c', '''import socket
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('198.18.0.2',8124));s.listen(1)
c,_=s.accept()
while True:
 d=c.recv(4096)
 if not d:break
 c.sendall(d)
'''])
for attempt in range(50):
    tcp = socket.socket()
    tcp.settimeout(5)
    try:
        tcp.connect(('198.18.0.2', 8124))
        break
    except OSError:
        tcp.close()
        time.sleep(.1)
else:
    raise RuntimeError('cross-namespace probe server did not start')

def advance():
    global counter
    while True:
        time.sleep(.1)
        with lock:
            counter += 1
threading.Thread(target=advance, daemon=True).start()

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with lock:
            tcp.sendall(nonce.encode())
            reply = tcp.recv(4096).decode()
            attr = marker.stat()
            result = {'nonce': nonce, 'counter': counter, 'pid': os.getpid(),
                      'unlinked_file': os.pread(fd, 1000, 0).decode(), 'file_offset': os.lseek(fd, 0, os.SEEK_CUR),
                      'marker': marker.read_text(), 'uid': attr.st_uid, 'gid': attr.st_gid, 'mode': oct(attr.st_mode & 0o777),
                      'cross_netns_tcp': reply, 'abstract_queued': unix_server.recv(100, socket.MSG_PEEK).decode()}
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global counter
        with lock:
            counter = 1000000
            marker.write_text('changed-after-checkpoint')
        self.send_response(204)
        self.end_headers()

http.server.HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
