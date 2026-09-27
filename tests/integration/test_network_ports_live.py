"""Real helper ownership under competing binds, and concurrent SDK launches."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import errno
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

from sandweave import CPU, Memory, Sandbox
from sandweave.sandbox import workspace
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]

PORTS = (80, 8080, 8000, 5901, 22, 23799, 43123)


def space(growth):
    for path in (workspace.local_parent(), workspace.home()):
        path.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(path)
        if disk.free - growth < disk.total * .15:
            pytest.skip('network qualification would leave less than 15% free')


def bound(port=0):
    sock = socket.socket()
    try:
        sock.bind(('127.0.0.1', port))
        sock.listen()
        return sock
    except BaseException:
        sock.close()
        raise


def assert_owned(ports):
    assert len(ports) == len(set(ports.values()))
    for port in ports.values():
        with pytest.raises(OSError) as error:
            with bound(port):
                pass
        assert error.value.errno == errno.EADDRINUSE


def stop(child):
    if child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)


def test_failed_helper_releases_ports_and_does_not_overwrite_report(tmp_path, monkeypatch):
    space(1024**3)
    root = workspace.prepare()
    monkeypatch.syspath_prepend(str(root / 'scripts'))
    import runtime_tools
    monkeypatch.setenv('PATH', workspace.tool_path())
    command = runtime_tools.command(root, tmp_path, 'passt', '-f', '-4',
        '-s', 'missing/failed.sock', '--tcp-port-report', 'ports.json',
        '-t', '127.0.0.1/0:23799', memory_limit=True, cwd=tmp_path)
    failed = subprocess.run(command, cwd=tmp_path, capture_output=True, timeout=15)
    assert failed.returncode != 0
    path = tmp_path / 'ports.json'
    original = path.read_bytes()
    ports = json.loads(original)
    assert set(ports) == {'23799'}
    assert path.stat().st_mode & 0o777 == 0o600
    for port in ports.values():
        with bound(port):
            pass
    failed = subprocess.run(command, cwd=tmp_path, capture_output=True, timeout=15)
    assert failed.returncode != 0 and b"Couldn't create TCP port report" in failed.stderr
    assert path.read_bytes() == original


def test_helpers_own_all_reported_ports_until_exit(tmp_path, monkeypatch):
    space(1024**3)
    # Includes the actual Apptainer helper execution path, not a simulated bind.
    root = workspace.prepare()
    monkeypatch.syspath_prepend(str(root / 'scripts'))
    import runtime_tools
    monkeypatch.setenv('PATH', workspace.tool_path())
    barrier = threading.Barrier(64)
    children, outputs, reserved = [], [], []
    done = threading.Event()
    def contention():
        while not done.wait(.001):
            with bound():
                pass
    churn = threading.Thread(target=contention)
    try:
        reserved = [bound() for _ in range(1024)]
        churn.start()
        def start(index):
            directory = tmp_path / str(index)
            directory.mkdir()
            output = (directory / 'helper.log').open('wb')
            outputs.append(output)
            forwards = [arg for port in PORTS for arg in ('-t', f'127.0.0.1/0:{port}')]
            command = runtime_tools.command(root, tmp_path, 'passt', '-f', '-1', '-4',
                '-s', 'passt.sock', '--tcp-port-report', 'ports.json', *forwards,
                memory_limit=True, cwd=directory)
            barrier.wait(timeout=30)
            child = subprocess.Popen(command, cwd=directory, stdout=output, stderr=subprocess.STDOUT)
            children.append(child)
            deadline = time.monotonic() + 45
            while not (directory / 'passt.sock').exists():
                assert child.poll() is None, (directory / 'helper.log').read_text()
                assert time.monotonic() < deadline, 'network helper never became ready'
                time.sleep(.01)
            ports = json.loads((directory / 'ports.json').read_text())
            assert set(ports) == set(map(str, PORTS))
            assert_owned(ports)
            return ports
        with ThreadPoolExecutor(64) as executor:
            mappings = list(executor.map(start, range(64)))
        all_ports = [port for ports in mappings for port in ports.values()]
        assert len(all_ports) == len(set(all_ports)) == 448
        assert not set(all_ports) & {sock.getsockname()[1] for sock in reserved}
        for child in children:
            assert child.poll() is None
            stop(child)
        for port in all_ports:
            with bound(port):
                pass
        print(json.dumps({'helpers': 64, 'unique_owned_ports': len(all_ports),
                          'competing_listeners': len(reserved), 'released_after_exit': True}))
    finally:
        done.set()
        if churn.ident is not None:
            churn.join(timeout=10)
        for child in children:
            stop(child)
        for output in outputs:
            output.close()
        for sock in reserved:
            sock.close()


def test_concurrent_sandbox_ports_and_restore(tmp_path):
    space(12 * 1024**3)
    barrier = threading.Barrier(50)
    envs, samples = [], []
    started = time.monotonic()
    try:
        with Sandbox(cpu=CPU(1), memory=Memory('256MiB', '256MiB')) as control:
            def start(index):
                barrier.wait(timeout=30)
                env = Sandbox(cpu=CPU(1), memory=Memory('256MiB', '256MiB'))
                envs.append(env)
                assert env.run(f'echo sandbox-{index}', timeout=30, check=True).stdout == f'sandbox-{index}\n'
                return env.status()['runtime_status']['ports']
            with ThreadPoolExecutor(50) as executor:
                pending = [executor.submit(start, index) for index in range(50)]
                while not all(future.done() for future in pending):
                    space(8 * 1024**3)
                    before = time.monotonic()
                    assert control.run('echo responsive', timeout=10, check=True).stdout == 'responsive\n'
                    samples.append(time.monotonic() - before)
                    time.sleep(.1)
                mappings = [future.result() for future in pending]
            mappings.append(control.status()['runtime_status']['ports'])
            all_ports = [port for ports in mappings for port in ports.values()]
            assert len(all_ports) == len(set(all_ports))
            for ports in mappings:
                assert_owned(ports)
            envs[0].run('echo restored > /workspace/port-probe', check=True)
            saved = envs[0].snapshot(state='filesystem')
            with Sandbox(snapshot=saved) as restored:
                assert restored.run('cat /workspace/port-probe', check=True).stdout == 'restored\n'
                restored_ports = restored.status()['runtime_status']['ports']
                assert not set(restored_ports.values()) & set(all_ports)
                assert_owned(restored_ports)
            report = {'concurrent_launches': 50, 'unique_owned_ports': len(all_ports),
                      'total_seconds_including_restore': time.monotonic() - started,
                      'unrelated_commands': len(samples),
                      'unrelated_median_seconds': sorted(samples)[len(samples) // 2],
                      'unrelated_maximum_seconds': max(samples), 'restore': 'passed'}
            (tmp_path / 'launch-concurrency.json').write_text(json.dumps(report, indent=2))
            print(json.dumps(report))
    finally:
        def cleanup(env):
            try:
                env.terminate()
            finally:
                env.close()
        with ThreadPoolExecutor(16) as executor:
            for future in as_completed([executor.submit(cleanup, env) for env in envs]):
                future.result()
        connection = local_connection()
        try:
            connection.call('_shutdown_if_idle')
        finally:
            connection.close()
