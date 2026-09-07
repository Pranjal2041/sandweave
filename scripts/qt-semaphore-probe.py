#!/usr/bin/env python3
"""Check Qt's multi-token semaphore wakeup without graphics or Resolve."""
import ctypes as C
import json
import threading
import time

qt = C.CDLL('libQt5Core.so.5')
storage = (C.c_uint64 * 2)()
construct = getattr(qt, '_ZN10QSemaphoreC1Ei')
acquire = getattr(qt, '_ZN10QSemaphore7acquireEi')
release = getattr(qt, '_ZN10QSemaphore7releaseEi')
destroy = getattr(qt, '_ZN10QSemaphoreD1Ev')
for function in (construct, acquire, release):
    function.argtypes = [C.c_void_p, C.c_int]
    function.restype = None
destroy.argtypes = [C.c_void_p]
destroy.restype = None
construct(storage, 0)


def producer():
    time.sleep(0.2)
    release(storage, 1)
    release(storage, 1)
    print(json.dumps({'phase': 'released', 'state': hex(storage[0])}), flush=True)


worker = threading.Thread(target=producer)
worker.start()
acquire(storage, 2)
worker.join()
destroy(storage)
print(json.dumps({'phase': 'acquired', 'passed': True}), flush=True)
