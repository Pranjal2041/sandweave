#!/usr/bin/env python3
"""Build the opt-in SDL joystick proxy using the pinned SDL2 dynamic API ABI."""
import hashlib
from pathlib import Path
import subprocess
import urllib.request

lab = Path(__file__).resolve().parent.parent
root = lab / 'downloads/sdl-gamepad-proxy/2.30.9'
root.mkdir(parents=True, exist_ok=True)
header = root / 'SDL_dynapi_procs.h'
# 2.30.9 adds rumble's slots; its Linux slot list keeps 2.0.8's as an exact prefix.
url = 'https://raw.githubusercontent.com/libsdl-org/SDL/release-2.30.9/src/dynapi/SDL_dynapi_procs.h'
expected = 'b62e701d15d3f26760ce27015a2a8aaa3cf38424b9235b4a23b742063710893c'
if not header.exists():
    header.write_bytes(urllib.request.urlopen(url, timeout=30).read())
if hashlib.sha256(header.read_bytes()).hexdigest() != expected:
    raise SystemExit('SDL ABI header hash mismatch')
output = lab / 'tools/gpu/vr/libsdl-gamepad-proxy.so'
output.parent.mkdir(parents=True, exist_ok=True)
subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                '-Wl,-Bsymbolic', '-I', str(root),
                str(lab / 'scripts/sdl-gamepad-proxy.c'), '-ldl', '-o', str(output)], check=True)
print(output)
