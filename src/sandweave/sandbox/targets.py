"""Explicit worker placement. A client connection never owns unrelated jobs."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from .connection import Connection
from .errors import ResourceUnavailable
from .workspace import home, locked


def local_connection():
    key = socket.gethostname() + '-' + os.environ.get('SLURM_JOB_ID', 'local')
    directory = home() / 'connections' / key
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = directory / 'worker.json'
    with locked(directory / 'startup.lock'):
        if metadata.exists():
            info = json.loads(metadata.read_text())
            connection = Connection('127.0.0.1', info['port'], info['token'])
            try:
                connection.call('ping')
                return connection
            except Exception:
                connection.close()
                metadata.unlink()
        with (directory / 'worker.log').open('ab') as log:
            environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2]) +
                           os.pathsep + os.environ.get('PYTHONPATH', '')}
            child = subprocess.Popen([sys.executable, '-m', 'sandweave.sandbox.worker',
                                      '--metadata', str(metadata)], stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                     env=environment)
        deadline = time.monotonic() + 180
        while not metadata.exists():
            if child.poll() is not None:
                raise ResourceUnavailable('worker failed to start: ' + (directory / 'worker.log').read_text()[-5000:])
            if time.monotonic() >= deadline:
                raise TimeoutError('worker preparation is still running; inspect ' + str(directory / 'worker.log'))
            time.sleep(.05)
        info = json.loads(metadata.read_text())
        connection = Connection('127.0.0.1', info['port'], info['token'])
        connection.call('ping')
        return connection


def connect(target=None):
    if target in (None, 'local'):
        return local_connection()
    raise ResourceUnavailable('target adapter is not registered: ' + str(target))
