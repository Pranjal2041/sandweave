#!/usr/bin/env python3
"""Measure notification latency after a CUDA stream reaches a host callback."""
import ctypes as C
import argparse
import json
import statistics
import threading
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--iterations', type=int, default=40)
parser.add_argument('--idle-ms', type=float, default=20,
                    help='allow the CUDA event thread to sleep before enqueueing work')
args = parser.parse_args()
if args.iterations < 1 or args.idle_ms < 0:
    parser.error('iterations must be positive and idle-ms nonnegative')

cuda = C.CDLL('libcuda.so.1')
ptr = C.c_void_p


def call(name, types, *values):
    function = getattr(cuda, name)
    function.restype, function.argtypes = C.c_int, types
    result = function(*values)
    assert result == 0, (name, result)


call('cuInit', [C.c_uint], 0)
context, stream = ptr(), ptr()
call('cuCtxCreate_v2', [C.POINTER(ptr), C.c_uint, C.c_int], C.byref(context), 0, 0)
call('cuStreamCreate', [C.POINTER(ptr), C.c_uint], C.byref(stream), 1)
Callback = C.CFUNCTYPE(None, ptr, C.c_int, ptr)
HostFunction = C.CFUNCTYPE(None, ptr)
received = []
ready = threading.Event()
memory = C.c_uint64()
call('cuMemAlloc_v2', [C.POINTER(C.c_uint64), C.c_size_t], C.byref(memory), 32 * 1024**2)


@Callback
def callback(stream, status, data):
    received.append((time.perf_counter(), status))
    ready.set()


@HostFunction
def host_function(data):
    received.append((time.perf_counter(), 0))
    ready.set()


results = {}
for name in ('cuStreamAddCallback', 'cuLaunchHostFunc'):
    samples = []
    for attempt in range(args.iterations + 5):
        time.sleep(args.idle_ms / 1000)
        received.clear()
        ready.clear()
        start = time.perf_counter()
        call('cuMemsetD8Async', [C.c_uint64, C.c_ubyte, C.c_size_t, ptr], memory, attempt % 256, 32 * 1024**2, stream)
        if name == 'cuStreamAddCallback':
            call(name, [ptr, Callback, ptr, C.c_uint], stream, callback, None, 0)
        else:
            call(name, [ptr, HostFunction, ptr], stream, host_function, None)
        assert ready.wait(5), 'CUDA host callback was not delivered within 5 seconds'
        assert len(received) == 1 and received[0][1] == 0, received
        if attempt >= 5:
            samples.append((received[0][0] - start) * 1000)
    results[name] = {'median_ms': statistics.median(samples), 'max_ms': max(samples), 'samples_ms': samples}
call('cuStreamSynchronize', [ptr], stream)
data = C.c_ubyte()
call('cuMemcpyDtoH_v2', [ptr, C.c_uint64, C.c_size_t], C.byref(data), memory, 1)
assert data.value == (args.iterations + 4) % 256, data.value
call('cuMemFree_v2', [C.c_uint64], memory)
call('cuStreamDestroy_v2', [ptr], stream)
call('cuCtxDestroy_v2', [ptr], context)
print(json.dumps({'passed': True, 'idle_ms': args.idle_ms, 'results': results}), flush=True)
