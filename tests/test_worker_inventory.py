"""Historical records must not add I/O to worker health checks."""
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

from sandweave.sandbox.worker import Worker
from sandweave.sandbox.management import Management


def bare_worker(tmp_path):
    worker = Worker.__new__(Worker)
    worker.root, worker.records = tmp_path, tmp_path/'sandboxes'
    worker.records.mkdir()
    worker.guard, worker.locks, worker.active = threading.RLock(), {}, set()
    worker.memory_budget = 1024**3
    worker.runtime = SimpleNamespace(status=lambda identity: {'status': 'running'})
    worker.management = Management(worker)
    worker.management.sampler.sample = lambda *args: {}
    return worker


def test_inventory_reads_only_active_records_under_concurrent_transitions(tmp_path):
    worker = bare_worker(tmp_path)
    spec = {'resources': {'memory': {'guest': 1, 'runtime': 1}, 'gpu': None}}
    for number in range(1000):
        worker.write(dict(id=f'old-{number}', state='terminated', spec=spec))
    worker.write(dict(id='live', state='ready', spec=spec))
    read = worker.read
    seen = []
    def checked(identity):
        assert not identity.startswith('old-')
        seen.append(identity)
        return read(identity)
    worker.read = checked
    def cycle(number):
        identity = 'new-' + str(number)
        for state in ('creating', 'ready', 'terminated'):
            with worker.lock(identity):
                worker.write(dict(id=identity, state=state, spec=spec))
    with ThreadPoolExecutor(8) as executor:
        futures = [executor.submit(cycle, n) for n in range(50)]
        for _ in range(10):
            assert any(r['id'] == 'live' for r in worker.management.inventory()['live'])
        for future in futures:
            future.result()
    assert worker.active == {'live'}
    seen.clear()
    worker.management.inventory()
    assert seen == ['live']


def test_worker_rebuilds_active_index_once_after_restart(tmp_path, monkeypatch):
    worker = bare_worker(tmp_path)
    for identity, state in [('ready', 'ready'), ('failed', 'failed'), ('stopped', 'stopped')]:
        worker.write(dict(id=identity, state=state, spec={}))
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path/'home'))
    monkeypatch.setattr('sandweave.sandbox.runtimes.router.Runtime', lambda root: SimpleNamespace(root=root))
    monkeypatch.setattr(threading.Thread, 'start', lambda self: None)
    restarted = Worker(tmp_path)
    try:
        assert restarted.active == {'ready', 'failed'}
    finally:
        restarted.deletions.close()
