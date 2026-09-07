#!/usr/bin/env python3
"""Verify a four-SM MPS client and a one-MiB GPU memory round trip."""
import ctypes as C
import json
import os

assert os.environ.get('CUDA_MPS_SM_PARTITION'), 'Set the private MPS partition first'
cuda = C.CDLL('libcuda.so.1')
ptr = C.c_void_p


def call(name, types, *values):
    function = getattr(cuda, name)
    function.argtypes, function.restype = types, C.c_int
    result = function(*values)
    assert result == 0, (name, result)


call('cuInit', [C.c_uint], 0)
context = ptr()
call('cuCtxCreate_v2', [C.POINTER(ptr), C.c_uint, C.c_int], C.byref(context), 0, 0)
try:
    sms = C.c_int()
    call('cuDeviceGetAttribute', [C.POINTER(C.c_int), C.c_int, C.c_int],
         C.byref(sms), 16, 0)  # CU_DEVICE_ATTRIBUTE_MULTIPROCESSOR_COUNT
    memory = C.c_uint64()
    call('cuMemAlloc_v2', [C.POINTER(C.c_uint64), C.c_size_t],
         C.byref(memory), 1024**2)
    try:
        call('cuMemsetD8_v2', [C.c_uint64, C.c_ubyte, C.c_size_t],
             memory, 0x5a, 1024**2)
        data = (C.c_ubyte * 1024**2)()
        call('cuMemcpyDtoH_v2', [ptr, C.c_uint64, C.c_size_t], data, memory, len(data))
        assert all(value == 0x5a for value in data), 'GPU readback mismatch'
    finally:
        call('cuMemFree_v2', [C.c_uint64], memory)
    assert sms.value == 4, sms.value
    print(json.dumps({'passed': True, 'visible_sms': sms.value, 'verified_bytes': len(data)}))
finally:
    call('cuCtxDestroy_v2', [ptr], context)
