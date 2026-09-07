# DaVinci Resolve on the no-KVM GPU guest

Tested 2026-09-07 on `babel-u5-28`, Slurm allocation 10333558. This is the
standalone lab; Gym Anything's repository is unchanged. The runtime remains
unprivileged Apptainer + gVisor systrap + nvproxy, with `/dev/kvm` absent.

## Actual acceptance and remaining limits

DaVinci Resolve **21.0.4.0005**, free Linux edition, starts through the GNOME
application menu. Its log identifies **CUDA** processing and **OpenGL 4.5 on
NVIDIA L40S/PCIe/SSE2**. The vendor application binaries are unchanged.

Through VNC, imported a five-second 1280x720, 24 fps DNxHR LB clip with a
440 Hz PCM tone, created a timeline, played it, set primary saturation to zero,
saved the project, closed Resolve, launched it from the menu and reopened the
project. The timeline and desaturation survived. Exported the complete timeline
to 1920x1080 ProRes 422 HQ with stereo 24-bit PCM. Resolve reported **8 seconds**
for the successful export. This is one short compatibility trial, not a benchmark.

Independent FFmpeg decoding verified all **120 frames**, five seconds of video
and audio, changing image content, zero mean RGB channel spread in three sampled
frames, and the original **440 Hz** tone. The exported frame was also opened and
visually inspected. The test's DNxHR decoding and ProRes encoding are not evidence
of NVDEC/NVENC hardware codec acceleration; the GPU evidence is CUDA processing
and the NVIDIA OpenGL renderer.

**Playback remains below real time:** the Resolve viewer reported roughly
8.7–8.9 fps, both before and after the audio setup correction. Frames advance
and scrubbing works, but 24 fps playback is not qualified. A subsequent native
control reached 24 fps on this same GPU, including with four-core affinity.
Profiling found spare GPU compute and fast VirtualGL readback/blitting, while
a fresh guest reproduced the slowdown. The exact remaining cause is unresolved;
see [the measurements and control differences](resolve-playback-performance.md).
Other jobs occupy most of this GPU's VRAM, but no other jobs or devices were changed.

The test does not cover arbitrary codecs, plugins, long projects, games, multi-GPU
operation or CUDA video codec acceleration. Resolve logged an NvEncodeAPI load
failure; it did not prevent this ProRes export. The 610.43.02 driver still uses
the lab's explicit unsupported-driver allowance. GPU checkpointing is deferred;
CPU-only snapshots retain their existing runtime pins. VNC provides video and
input; the virtual audio sink does not forward sound to the Mac.

## Two independent blockers resolved

### Futex ABI bug prevented the main editor from opening

The old runtime stopped in Qt's `QSemaphore::acquire(2)` while parallel image
conversion workers had already released their tokens. A minimal ctypes program
using Resolve's bundled Qt, with no graphics or Resolve process, reproduced it:
native Linux passed; old gVisor timed out after eight seconds.

That Qt binary uses `FUTEX_WAKE_OP` to clear bit 31 of the upper semaphore word
and conditionally wake multi-token waiters when the **old signed word < 0**.
gVisor's `atomicOp` compared `uint32` values: `0x80000001 < 0` was false, so it
cleared the bit without waking the waiter. The semaphore could contain available
tokens while its consumer stayed asleep. This is an implementation bug, not a
requirement to emulate application instructions or a fundamental systrap limit.

Engine commit `1bfaec6` fixes signed comparisons, sign extension of the two
12-bit operands, and Linux's masking of out-of-range shift counts. These follow
[`futex_atomic_op_inuser` in Linux](https://github.com/torvalds/linux/blob/v6.12/kernel/futex/waitwake.c#L203).
No Qt or Resolve binary patch is needed.

Validation:

- Added deterministic engine tests for all six comparisons across signed
  boundaries, negative arithmetic operands, shifted operands, and Qt's actual
  wake pattern with both private and shared futexes. Failed before the fix;
  the complete futex unit-test target passed afterward.
- Standalone syscall probe: **56/56** checks pass on native Linux and the fixed
  guest; **39/56 failed** on the old guest.
- The same bundled-Qt semaphore probe now exits successfully in the fixed guest.
- All **nine existing upstream WakeOp syscall tests** pass in both native
  Apptainer and the fixed guest.
- Full runtime build and six existing GPU allocation/identity tests pass.

### Missing desktop audio backend stalled export

The base desktop had PipeWire but lacked its PulseAudio endpoint and the ALSA
PulseAudio plugin. Resolve repeatedly tried an absent physical ALSA card. Its
first ProRes render stayed at the first frame and wrote no output file. The
UI remained responsive and allowed cancelling the job and quitting.

Installed `libasound2-plugins`, `pipewire-pulse` and `pulseaudio-utils`; enabled
the packaged default ALSA-to-PulseAudio configuration and the user socket.
PipeWire supplies `auto_null` when no physical card exists. After restarting
Resolve, its audio stream connected to that sink and the same queued render
completed in eight seconds with the correct audio. This establishes the working
configuration; it does not separately prove the internal Fairlight wait path.
The audio correction did **not** fix the measured playback frame rate.

The root installer also leaves Resolve attempting to create a new
`/opt/resolve/Apple Immersive/Calibration` directory as user `ga`; initially this
failed with EACCES before onboarding. Setup creates this directory plus the
writable `Extras` and `logs` directories with owner `ga`. Other installation
files remain root-owned. The full setup helper passed in disposable
`resolve-setup-check2`, which exited after installation.

## Reproduce

The registration worked despite the blank browser page: the Mac download finished,
and its signed official Blackmagic URL returned HTTP 200. The 4,028,428,345-byte
archive was downloaded directly to the cluster, hashed and extracted successfully.
Signed URLs and installers are ignored, not committed.

Archive: `downloads/DaVinci_Resolve_21.0.4_Linux.zip`
SHA256: `d0bbc5bc09aaecaa693e22b2a037f42da8c984a5e9f3e1ff77c7db681325b50f`

The staging helper checks this archive before initial extraction and uses the
official AppImage extraction mode, requiring no FUSE mount. Subsequent staging
uses its completed extraction marker. It also generates the test movie.

```bash
cd ~/scratch/general-vm
scripts/stage-resolve.sh
python scripts/stage-gvisor.py
python scripts/run-gvisor.py --detach --gpu 0 --guest-gs \
  --memory-mib 49152 --cgroup v1 --no-runtime-debug my-resolve -- \
  sh -c '/usr/local/bin/engine-resolve-setup && exec /sbin/init'
```

After the desktop boots, launch **DaVinci Resolve** from Activities. The
desktop entry uses `engine-resolve`, which invokes the existing NVIDIA/VirtualGL
wrapper with `-nodl` for CUDA/OpenGL interop. Use a 1920x1080 desktop to expose
all Resolve controls; the lab's 1280x800 default clips some controls. For this
lab's TigerVNC display, run inside the guest as `ga`:

```bash
DISPLAY=:1 XAUTHORITY=/home/ga/.Xauthority xrandr --output VNC-0 --mode 1920x1080
```

Test media is `/home/ga/Videos/resolve-test.mov`. Follow the acceptance sequence
above; after copying the resulting ProRes movie out, validate it with:

```bash
python scripts/verify-resolve-export.py PATH_TO_EXPORTED_MOVIE
```

The currently open desktop is **resolve-gpu2**, with four advertised CPUs,
48 GiB guest-page budget, 1 GiB runtime guard, weight 100 on the inherited
28-CPU pool, internet policy and GPU minor 0. GPU UUID:
`GPU-203c2df2-d777-ba3e-e218-dfc79347f5a5` (host NVML index 3).
Node VNC port **40651** is forwarded to **127.0.0.1:5912** on the Mac; password
`labvnc01`. These ports are session-specific. Project **No KVM GPU Test** and its
export `/home/ga/Videos/no-kvm-gpu-desaturated.mov` remain in that guest.

At the user's request, `moodle-user1`, `earth-user1`, `ready-clone2`, `gpu-ready1`
and `gpu-apps1` were stopped. The first Resolve guest was replaced with the
fixed-engine guest; no older desktop is kept running. Historical images and
snapshots remain available.

## Evidence

- [Kernel test result](gpu-evidence/futex-after-fix.txt),
  [native syscall checks](gpu-evidence/futex-native.json),
  [fixed guest checks](gpu-evidence/futex-gvisor-after.json),
  [old guest failures](gpu-evidence/futex-gvisor-before.json).
- [Qt native / before / after](gpu-evidence/qt-semaphore-results.txt),
  [upstream native tests](gpu-evidence/futex-upstream-native.txt),
  [upstream guest tests](gpu-evidence/futex-upstream-gvisor.txt).
- [CUDA/OpenGL log](gpu-evidence/resolve-runtime.txt),
  [color adjustment](gpu-evidence/resolve-color-desaturated.png),
  [completed export](gpu-evidence/resolve-export-complete.png),
  [decoded output](gpu-evidence/resolve-export-frame.png),
  [output validation](gpu-evidence/resolve-export-verification.json),
  [playback limitation](gpu-evidence/resolve-playback-after-audio.png).
