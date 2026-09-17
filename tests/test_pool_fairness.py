"""A later caller cannot hold the only slot while an earlier caller waits."""
from concurrent.futures import ThreadPoolExecutor
import threading

from sandweave.sandbox.pool import Pool


def test_waiting_leases_receive_capacity_in_arrival_order(monkeypatch):
    class Env:
        spec = {}
        def terminate(self):
            pass
        def close(self):
            pass

    pool = Pool(size=1)
    pool.started = True
    monkeypatch.setattr(pool, '_sandbox', lambda target: Env())
    entered = [threading.Event() for _ in range(16)]
    acquired = [threading.Event() for _ in entered]
    release = [threading.Event() for _ in entered]
    caller = threading.local()
    wait = pool.condition.wait
    def waiting(timeout):
        entered[caller.index].set()
        return wait(timeout)
    monkeypatch.setattr(pool.condition, 'wait', waiting)
    def consume(index):
        caller.index = index
        with pool.acquire(timeout=10):
            acquired[index].set()
            assert release[index].wait(10)

    held = pool.acquire()
    held.__enter__()
    try:
        with ThreadPoolExecutor(16) as executor:
            futures = []
            try:
                for index in range(16):
                    futures.append(executor.submit(consume, index))
                    assert entered[index].wait(3)
                held.__exit__(None, None, None)
                held = None
                for index in range(16):
                    assert acquired[index].wait(3), f'caller {index} was bypassed'
                    assert not any(event.is_set() for event in acquired[index + 1:])
                    release[index].set()
                for future in futures:
                    future.result(3)
            finally:
                for event in release:
                    event.set()
    finally:
        if held is not None:
            held.__exit__(None, None, None)
        pool.close()
    assert not pool.active and not pool.all and not pool.waiters
