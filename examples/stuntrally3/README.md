# Stunt Rally 3

Run the free, standalone Linux release of [Stunt Rally 3.3](https://sourceforge.net/projects/stuntrally/files/3.3/)
in a GPU desktop sandbox. It starts a single-player race on the Beach track
at 1920×1080. No Steam account or game purchase is required.

Run these commands **on the Linux worker with your allocated NVIDIA GPU**.
The example uses Sandweave's GNOME template, gVisor and VirtualGL's EGL backend.
It needs four available CPUs and 7 GiB for the sandbox and runtime, plus space
for the runtime, game and optional recording. First use downloads dependencies
and installs packages inside the guest.

```bash
uv pip install 'sandweave[desktop]>=0.2.17'
sh examples/stuntrally3/prepare.sh /scratch/stuntrally3
python examples/stuntrally3/run.py \
  --game-dir /scratch/stuntrally3/StuntRally-3.3-Linux \
  --virtualgl /scratch/stuntrally3/virtualgl_3.1.5_amd64.deb \
  --output /scratch/stuntrally3/session
```

Choose writable paths on your worker; `/scratch` is an example. The output
directory must be new. The script prints `env.info`, including the VNC address
and password, and saves it in the private `session/sandbox.json` file. Forward
the VNC port using your normal connection to that worker.

Use the arrow keys to accelerate, brake and steer. Backspace resets the car;
Escape opens the game menus. The script keeps the sandbox open for an hour
by default. Change `--hold-seconds` or press Ctrl+C to end it.

Add `--record` to save a cursor-free desktop recording, capture timestamps and
dropped-frame counters under `session/recording/` when the script exits.
The requested capture rate is 30 FPS; it is independent of the game's render
rate. Recording consumes additional CPU time. This is a conventional desktop
game example, not a VR or stereo demo.

The same keyboard controls work from Python:

```python
import time

env.desktop.keyboard.down("Up")
try:
    time.sleep(1)
finally:
    env.desktop.keyboard.up("Up")
frame = env.desktop.screenshot()
```

`run.py` keeps the extracted game read-only and writes preferences inside the
guest. It sets the renderer and game configuration before launching, so the
first-run graphics dialog does not interrupt the race. The game binary is
launched directly: the upstream wrapper replaces `LD_LIBRARY_PATH`, which
would discard the staged NVIDIA driver path.

The download script pins checksums for the official game archive and VirtualGL
package. It does not install host software or change your NVIDIA driver.
