"""Opt-in acceptance for the engine and transport used by OSWorld."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import threading
import time

import pytest

from sandweave import Sandbox, Memory, Benchmark
from sandweave.benchmarks import Evaluation, TaskSpec
from sandweave.benchmarks.transport import GuestPort
from sandweave.templates.resolve import resolve

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_OSWORLD_INTEGRATION'), reason='explicit OSWorld candidate worker required')]


def test_console_permissions_primitives_and_live_restore():
    recipe = resolve('coding')
    recipe['runtime_options']['virtual_consoles'] = True
    with Sandbox(template=recipe, memory=Memory('256MiB', '256MiB')) as env:
        env.files.upload(Path(__file__).resolve().parents[2] / 'scripts/probe-osworld-runtime.py', '/tmp/probe.py')
        result = env.run('python /tmp/probe.py --consoles', timeout=30, check=True)
        assert len(result.stdout.splitlines()) == 3
        env.files.write_text('/tmp/value', 'original')
        env.run("python -c 'import os,stat,fcntl; os.mknod(\"/dev/tty10\",stat.S_IFCHR|0o600,os.makedev(4,10)); "
                "f=os.open(\"/dev/tty10\",os.O_RDWR|os.O_NOCTTY); fcntl.ioctl(f,0x5606,10)'", check=True)
        snapshot = env.snapshot(state='memory')
        with Sandbox(snapshot=snapshot.id) as clone:
            assert clone.files.read_text('/tmp/value') == 'original'
            assert clone.run('cat /sys/class/tty/tty0/active', check=True).stdout == 'tty10\n'
            clone.files.write_text('/tmp/value', 'changed')
            assert env.files.read_text('/tmp/value') == 'original'


def test_concurrent_guest_service_routes_do_not_block_unrelated_commands():
    server = '''import socket, threading
s = socket.socket(); s.bind(('127.0.0.1', 17892)); s.listen(16)
def echo(c):
    with c:
        chunks=[]
        while data:=c.recv(65536): chunks.append(data)
        c.sendall(b''.join(chunks))
while True:
    c,_=s.accept(); threading.Thread(target=echo,args=(c,),daemon=True).start()
'''
    with Sandbox(memory=Memory('256MiB', '256MiB')) as guest, Sandbox(memory=Memory('256MiB', '256MiB')) as other:
        process = guest.exec(argv=['python', '-u', '-c', server])
        barrier = threading.Barrier(9)
        try:
            # Readiness uses the same user route as the subsequent connections.
            guest.run("python -c 'import socket,time; "
                      "exec(\"for i in range(100):\\n try:\\n  socket.create_connection((\\\"127.0.0.1\\\",17892)).close(); break\\n except OSError: time.sleep(.05)\")'", check=True)
            with GuestPort(guest, 17892) as port:
                def exchange(index):
                    data = bytes([index]) * (256 * 1024)
                    barrier.wait(10)
                    with socket.create_connection(('127.0.0.1', port), timeout=30) as peer:
                        peer.sendall(data)
                        peer.shutdown(socket.SHUT_WR)
                        received = bytearray()
                        while chunk := peer.recv(65536):
                            received.extend(chunk)
                    assert received == data
                with ThreadPoolExecutor(8) as executor:
                    jobs = [executor.submit(exchange, i) for i in range(8)]
                    barrier.wait(10)
                    started = time.monotonic()
                    for _ in range(20):
                        assert other.run('printf independent', timeout=10).stdout == 'independent'
                    unrelated_seconds = time.monotonic() - started
                    for job in jobs:
                        job.result(30)
            print(json.dumps({'connections': 8, 'bytes_each': 256 * 1024,
                              'unrelated_commands': 20, 'unrelated_seconds': unrelated_seconds}))
        finally:
            process.terminate()


def test_benchmark_maps_clean_live_leases_concurrently():
    class Arithmetic:
        tasks = tuple(TaskSpec(str(i), str(i)) for i in range(12))

        def prepare(self):
            return {'template': 'coding', 'memory': Memory('256MiB', '256MiB')}

        def setup(self, env, task):
            assert env.run('test ! -e /workspace/answer').returncode == 0

        def evaluate(self, env, task):
            passed = env.files.read_text('/workspace/answer') == str(int(task.id) * 2)
            return Evaluation(task.id, 100.0 if passed else 0.0, passed)

    barrier = threading.Barrier(4)
    identities = set()
    lock = threading.Lock()

    def agent(env, instruction):
        barrier.wait(timeout=120)
        with lock:
            assert env.id not in identities
            identities.add(env.id)
        result = env.run('python -c "print(' + instruction + ' * 2)"', check=True)
        env.files.write_text('/workspace/answer', result.stdout.strip())

    started = time.monotonic()
    with Benchmark(Arithmetic(), capacity=4) as bench:
        results = list(bench.map(agent))
    assert [result.task_id for result in results] == [str(i) for i in range(12)]
    assert all(result.passed for result in results)
    assert len(identities) == 12
    print(json.dumps({'tasks': 12, 'concurrent_leases': 4, 'seconds': time.monotonic() - started}))
