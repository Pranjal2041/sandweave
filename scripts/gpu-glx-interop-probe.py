#!/usr/bin/env python3
"""Exercise a root-visual OpenGL context and CUDA/OpenGL buffer sharing."""
import ctypes as C
import json
import os
import statistics
import sys
import time


class VisualInfo(C.Structure):
    _fields_ = [('visual', C.c_void_p), ('visualid', C.c_ulong),
                ('screen', C.c_int), ('depth', C.c_int), ('visual_class', C.c_int),
                ('red_mask', C.c_ulong), ('green_mask', C.c_ulong),
                ('blue_mask', C.c_ulong), ('colormap_size', C.c_int),
                ('bits_per_rgb', C.c_int)]


def function(library, name, result, *arguments):
    call = getattr(library, name)
    call.restype, call.argtypes = result, arguments
    return call


# Match a GUI executable linked to libGL, including under vglrun -nodl.
C.CDLL('libGL.so.1', mode=C.RTLD_GLOBAL)
x = gl = C.CDLL(None)
cuda = C.CDLL('libcuda.so.1')
ptr, integer, ulong = C.c_void_p, C.c_int, C.c_ulong
display = function(x, 'XOpenDisplay', ptr, C.c_char_p)(None)
assert display, 'DISPLAY is not accessible'
screen = function(x, 'XDefaultScreen', integer, ptr)(display)
root = function(x, 'XRootWindow', ulong, ptr, integer)(display, screen)
visual = function(x, 'XDefaultVisual', ptr, ptr, integer)(display, screen)
visual_id = function(x, 'XVisualIDFromVisual', ulong, ptr)(visual)
template, count = VisualInfo(visualid=visual_id, screen=screen), integer()
info = function(x, 'XGetVisualInfo', C.POINTER(VisualInfo), ptr, C.c_long,
                C.POINTER(VisualInfo), C.POINTER(integer))(display, 3, C.byref(template), C.byref(count))
assert count.value == 1
context = function(gl, 'glXCreateContext', ptr, ptr, C.POINTER(VisualInfo), ptr,
                   integer)(display, info, None, 1)
assert context, 'root visual has no GLX context'
window = function(x, 'XCreateSimpleWindow', ulong, ptr, ulong, integer, integer,
                  C.c_uint, C.c_uint, C.c_uint, ulong, ulong)(display, root, 0, 0, 32, 32, 0, 0, 0)
assert function(gl, 'glXMakeCurrent', integer, ptr, ulong, ptr)(display, window, context)
renderer = function(gl, 'glGetString', C.c_char_p, C.c_uint)(0x1F01).decode()
assert 'NVIDIA' in renderer, renderer
function(gl, 'glClearColor', None, C.c_float, C.c_float, C.c_float, C.c_float)(.25, .5, .75, 1)
function(gl, 'glClear', None, C.c_uint)(0x4000)
pixel = (C.c_ubyte * 4)()
function(gl, 'glReadPixels', None, integer, integer, integer, integer, C.c_uint,
         C.c_uint, ptr)(0, 0, 1, 1, 0x1908, 0x1401, pixel)
assert all(abs(a - b) <= 1 for a, b in zip(pixel, [64, 128, 191, 255])), list(pixel)
print(json.dumps({'phase': 'opengl', 'root_visual': hex(visual_id),
                  'renderer': renderer, 'pixel': list(pixel), 'passed': True}), flush=True)


if '--hold-gl' in sys.argv:
    print(json.dumps({'phase': 'holding_gl', 'pid': os.getpid()}), flush=True)
    while True:
        function(gl, 'glReadPixels', None, integer, integer, integer, integer, C.c_uint, C.c_uint, ptr)(0, 0, 1, 1, 0x1908, 0x1401, pixel)
        assert all(abs(a-b) <= 1 for a,b in zip(pixel, [64,128,191,255])), list(pixel)
        print(json.dumps({'phase': 'gl_alive', 'pid': os.getpid(), 'time': time.time(), 'pixel': list(pixel)}), flush=True)
        time.sleep(2)

def gl_extension(name, result, *arguments):
    address = function(gl, 'glXGetProcAddressARB', ptr, C.c_char_p)(name.encode())
    assert address, name
    return C.CFUNCTYPE(result, *arguments)(address)


def cu(name, arguments, *values):
    status = function(cuda, name, integer, *arguments)(*values)
    assert status == 0, (name, status)


buffer = C.c_uint()
gl_extension('glGenBuffers', None, integer, C.POINTER(C.c_uint))(1, C.byref(buffer))
gl_extension('glBindBuffer', None, C.c_uint, C.c_uint)(0x8892, buffer)
gl_extension('glBufferData', None, C.c_uint, C.c_ssize_t, ptr, C.c_uint)(0x8892, 256, None, 0x88E8)
cu('cuInit', [C.c_uint], 0)
device, cuda_context, resource = integer(), ptr(), ptr()
cu('cuDeviceGet', [C.POINTER(integer), integer], C.byref(device), 0)
cu('cuCtxCreate_v2', [C.POINTER(ptr), C.c_uint, integer], C.byref(cuda_context), 0, device)
cu('cuGraphicsGLRegisterBuffer', [C.POINTER(ptr), C.c_uint, C.c_uint], C.byref(resource), buffer, 2)
cu('cuGraphicsMapResources', [C.c_uint, C.POINTER(ptr), ptr], 1, C.byref(resource), None)
gpu_pointer, size = C.c_uint64(), C.c_size_t()
cu('cuGraphicsResourceGetMappedPointer_v2', [C.POINTER(C.c_uint64), C.POINTER(C.c_size_t), ptr],
   C.byref(gpu_pointer), C.byref(size), resource)
assert size.value == 256
cu('cuMemsetD8_v2', [C.c_uint64, C.c_ubyte, C.c_size_t], gpu_pointer, 42, size)
cu('cuGraphicsUnmapResources', [C.c_uint, C.POINTER(ptr), ptr], 1, C.byref(resource), None)
readback = (C.c_ubyte * 256)()
gl_extension('glGetBufferSubData', None, C.c_uint, C.c_ssize_t, C.c_ssize_t, ptr)(0x8892, 0, 256, readback)
assert bytes(readback) == b'*' * 256
if '--hold-interop' in sys.argv:
    print(json.dumps({'phase': 'holding_interop', 'pid': os.getpid()}), flush=True)
    while True:
        cu('cuGraphicsMapResources', [C.c_uint, C.POINTER(ptr), ptr], 1, C.byref(resource), None)
        cu('cuGraphicsUnmapResources', [C.c_uint, C.POINTER(ptr), ptr], 1, C.byref(resource), None)
        gl_extension('glGetBufferSubData', None, C.c_uint, C.c_ssize_t, C.c_ssize_t, ptr)(0x8892, 0, 256, readback)
        assert bytes(readback) == b'*' * 256
        print(json.dumps({'phase': 'interop_alive', 'pid': os.getpid(), 'time': time.time(), 'value': readback[0]}), flush=True)
        time.sleep(2)
if '--benchmark' in sys.argv:
    samples = {name: [] for name in ('map', 'memset', 'unmap', 'gl_read')}
    for attempt in range(110):
        before = time.perf_counter()
        cu('cuGraphicsMapResources', [C.c_uint, C.POINTER(ptr), ptr], 1, C.byref(resource), None)
        mapped = time.perf_counter()
        cu('cuGraphicsResourceGetMappedPointer_v2', [C.POINTER(C.c_uint64), C.POINTER(C.c_size_t), ptr],
           C.byref(gpu_pointer), C.byref(size), resource)
        cu('cuMemsetD8_v2', [C.c_uint64, C.c_ubyte, C.c_size_t], gpu_pointer, attempt, size)
        filled = time.perf_counter()
        cu('cuGraphicsUnmapResources', [C.c_uint, C.POINTER(ptr), ptr], 1, C.byref(resource), None)
        unmapped = time.perf_counter()
        gl_extension('glGetBufferSubData', None, C.c_uint, C.c_ssize_t, C.c_ssize_t, ptr)(0x8892, 0, 256, readback)
        after = time.perf_counter()
        assert bytes(readback) == bytes([attempt]) * 256
        if attempt >= 10:
            for name, elapsed in zip(samples, (mapped-before, filled-mapped, unmapped-filled, after-unmapped)):
                samples[name].append(elapsed * 1000)
    print(json.dumps({'phase': 'interop_latency', 'iterations': 100,
                      'milliseconds': {name: {'median': statistics.median(values), 'max': max(values)}
                                       for name, values in samples.items()}, 'passed': True}), flush=True)
cu('cuGraphicsUnregisterResource', [ptr], resource)
cu('cuCtxDestroy_v2', [ptr], cuda_context)
gl_extension('glDeleteBuffers', None, integer, C.POINTER(C.c_uint))(1, C.byref(buffer))
function(gl, 'glXMakeCurrent', integer, ptr, ulong, ptr)(display, 0, None)
function(gl, 'glXDestroyContext', None, ptr, ptr)(display, context)
function(x, 'XDestroyWindow', integer, ptr, ulong)(display, window)
function(x, 'XFree', integer, ptr)(info)
function(x, 'XCloseDisplay', integer, ptr)(display)
print(json.dumps({'phase': 'cuda_opengl_interop', 'bytes': 256, 'passed': True}), flush=True)
