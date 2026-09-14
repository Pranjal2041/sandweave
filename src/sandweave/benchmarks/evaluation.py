"""Reusable, isolated evaluator processes outside the agent's filesystem."""
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import threading
import time

from ..sandbox.workspace import home, locked, atomic_json
from ..setup_progress import run_logged
from ..templates.resolve import fingerprint


def environment():
    inherited = {key: value for key, value in os.environ.items()
                 if key not in ('CS_OSWORLD_ROOT', 'OSWORLD_ROOT', 'PYTHONPATH')}
    return {**inherited, 'CUA_OSWORLD_CACHE': str(home() / 'benchmarks/evaluators/osworld'),
            'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'}


def prepare(base, verifier):
    script = Path(__file__).with_name('evaluator_process.py')
    command = [str(script), str(verifier), '--check']
    env = environment()
    directory = home() / 'benchmarks/evaluators'
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / 'preflight.log'
    with locked(directory / 'prepare.lock'), log.open('w') as output:
        # Reuse an already installed compatible interpreter without changing it.
        probe = subprocess.run([sys.executable, *command], stdout=output, stderr=subprocess.STDOUT,
                               env=env, timeout=180)
        if probe.returncode == 0:
            return sys.executable
        key = fingerprint({'requirements': base.ENV_PLANE_PIP, 'torch': base.ENV_PLANE_TORCH_PIP,
                           'source': base.OSWORLD_EVALUATOR_COMMIT,
                           'python': list(sys.version_info[:2])})[:20]
        root = directory / key
        python = root / 'bin/python'
        uv = shutil.which('uv')
        if not python.is_file():
            run_logged(([uv, 'venv', '--python', sys.executable, str(root)] if uv else
                        [sys.executable, '-m', 'venv', str(root)]), home(),
                       label='Create OSWorld evaluator runtime', env=env)
        installer = [uv, 'pip', 'install', '--python', str(python)] if uv else [str(python), '-m', 'pip', 'install']
        if not (root / 'ready.json').is_file():
            cache_env = {**env, 'UV_CACHE_DIR': str(directory / 'downloads/uv'),
                         'PIP_CACHE_DIR': str(directory / 'downloads/pip')}
            run_logged([*installer, '--index-url', base.ENV_PLANE_TORCH_INDEX, *base.ENV_PLANE_TORCH_PIP],
                       home(), label='Install CPU evaluator dependencies', env=cache_env)
            run_logged([*installer, *base.ENV_PLANE_PIP], home(), label='Install OSWorld evaluators', env=cache_env)
        run_logged([str(python), *command], home(), label='Check OSWorld evaluator', env=env)
        versions = subprocess.check_output([str(python), '-m', 'pip', 'freeze'], env=env, text=True) if not uv else subprocess.check_output(
            [uv, 'pip', 'freeze', '--python', str(python)], env=env, text=True)
        atomic_json(root / 'ready.json', {'requirements': key, 'packages': versions})
        return str(python)


class Evaluators:
    def __init__(self, python, verifier):
        self.python, self.verifier = python, verifier
        self.idle, self.all = [], set()
        self.lock = threading.Condition()
        self.active = 0
        self.closed = False

    def _start(self):
        directory = home() / 'benchmarks/evaluators/logs'
        directory.mkdir(parents=True, exist_ok=True)
        import uuid
        path = directory / (uuid.uuid4().hex + '.log')
        with path.open('wb') as log:
            process = subprocess.Popen([self.python, '-u', str(Path(__file__).with_name('evaluator_process.py')),
                                        str(self.verifier)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=log, env=environment(), text=True, bufsize=1)
        try:
            if self._read(process, 180) != {'ready': True}:
                raise RuntimeError('OSWorld evaluator failed to start; log: ' + str(path))
        except BaseException:
            self._stop(process)
            raise
        return process

    @staticmethod
    def _read(process, timeout):
        if not select.select([process.stdout], [], [], timeout)[0]:
            raise TimeoutError('OSWorld evaluator timed out')
        line = process.stdout.readline()
        if not line:
            raise RuntimeError('OSWorld evaluator exited unexpectedly')
        return json.loads(line)

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()

    def run(self, request):
        with self.lock:
            if self.closed:
                raise RuntimeError('benchmark evaluator is closed')
            self.active += 1
            process = self.idle.pop() if self.idle else None
        reusable = False
        try:
            if process is None:
                process = self._start()
                with self.lock:
                    self.all.add(process)
            process.stdin.write(json.dumps(request) + '\n')
            process.stdin.flush()
            result = self._read(process, 600)
            reusable = True
            if 'error' in result:
                raise RuntimeError('OSWorld evaluator failed:\n' + result['error'])
            return result['result']
        finally:
            with self.lock:
                keep = reusable and not self.closed
                if keep:
                    self.idle.append(process)
                else:
                    self.all.discard(process)
            try:
                if not keep and process is not None:
                    self._stop(process)
            finally:
                with self.lock:
                    self.active -= 1
                    self.lock.notify_all()

    def close(self):
        with self.lock:
            self.closed = True
            self.lock.wait_for(lambda: self.active == 0)
            processes = list(self.all)
            self.idle.clear()
            self.all.clear()
        for process in processes:
            self._stop(process)
