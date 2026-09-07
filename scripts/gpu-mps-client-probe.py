#!/usr/bin/env python3
"""Verify an MPS client's SM count, CUDA kernel, memory round trip and optional cap."""
import argparse
import ctypes as C
import json
import os
import subprocess
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--inspect', action='store_true', help='compare CUDA and NVIDIA-SMI views')
parser.add_argument('--expect-sms', type=int, default=4)
parser.add_argument('--memory-limit-mib', type=int, help='verify that an allocation larger than this cap fails')
parser.add_argument('--hold-seconds', type=float, default=0, help='keep the validated context alive for concurrent probes')
args = parser.parse_args()

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
    assert sms.value == args.expect_sms, (sms.value, args.expect_sms)
    memory = C.c_uint64()
    call('cuMemAlloc_v2', [C.POINTER(C.c_uint64), C.c_size_t],
         C.byref(memory), 1024**2)
    try:
        call('cuMemsetD8_v2', [C.c_uint64, C.c_ubyte, C.c_size_t],
             memory, 0x5a, 1024**2)
        data = (C.c_ubyte * 1024**2)()
        call('cuMemcpyDtoH_v2', [ptr, C.c_uint64, C.c_size_t], data, memory, len(data))
        assert all(value == 0x5a for value in data), 'GPU readback mismatch'
        module, kernel = ptr(), ptr()
        ptx = C.create_string_buffer(b'''.version 8.0
.target sm_52
.address_size 64
.visible .entry write_value(.param .u64 destination) {
 .reg .u64 address;
 ld.param.u64 address, [destination];
 st.global.u32 [address], 1234567;
 ret;
}
''')
        call('cuModuleLoadData', [C.POINTER(ptr), ptr], C.byref(module), ptx)
        try:
            call('cuModuleGetFunction', [C.POINTER(ptr), ptr, C.c_char_p], C.byref(kernel), module, b'write_value')
            parameters = (ptr * 1)(C.cast(C.byref(memory), ptr))
            call('cuLaunchKernel', [ptr, *([C.c_uint] * 7), ptr, C.POINTER(ptr), C.POINTER(ptr)],
                 kernel, 1, 1, 1, 1, 1, 1, 0, None, parameters, None)
            call('cuCtxSynchronize', [])
            value = C.c_uint32()
            call('cuMemcpyDtoH_v2', [ptr, C.c_uint64, C.c_size_t], C.byref(value), memory, 4)
            assert value.value == 1234567, value.value
        finally:
            call('cuModuleUnload', [ptr], module)
        rejected = None
        if args.memory_limit_mib is not None:
            excess = C.c_uint64()
            rejected = cuda.cuMemAlloc_v2(C.byref(excess), (args.memory_limit_mib + 1) * 1024**2)
            if rejected == 0:
                call('cuMemFree_v2', [C.c_uint64], excess)
            assert rejected == 2, ('expected CUDA_ERROR_OUT_OF_MEMORY', rejected)
        if args.inspect:
            free, total = C.c_size_t(), C.c_size_t()
            call('cuMemGetInfo_v2', [C.POINTER(C.c_size_t), C.POINTER(C.c_size_t)],
                 C.byref(free), C.byref(total))
            gpu_uuid = os.environ['CUDA_MPS_SM_PARTITION'].split('/')[0]
            print(json.dumps({
                'cuda_free_mib': free.value / 1024**2,
                'cuda_total_mib': total.value / 1024**2,
                'nvidia_smi': subprocess.check_output(
                    ['nvidia-smi', '-i', gpu_uuid], text=True, timeout=10),
            }))
    finally:
        call('cuMemFree_v2', [C.c_uint64], memory)
    print(json.dumps({'passed': True, 'visible_sms': sms.value, 'verified_bytes': len(data),
                      'kernel_result': value.value, 'over_limit_cuda_status': rejected}), flush=True)
    time.sleep(args.hold_seconds)
finally:
    call('cuCtxDestroy_v2', [ptr], context)
