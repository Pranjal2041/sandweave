# Stunt Rally 3 acceptance, 2026-09-12

The standalone Linux Stunt Rally 3.3 release ran inside Sandweave's gVisor
GNOME/Xvnc sandbox with NVIDIA rendering. Acceleration, steering, braking and
car reset were exercised through `env.desktop.keyboard`. The Beach track loaded
without Steam or a game account. The recording includes acceleration and a
collision; it is a controls smoke test, not a completed race.

## Runtime

- Existing allocation: job `10411619`, `j-4e32c08877baf1e82149`, `babel-m9-32`.
- GPU: RTX PRO 6000 Blackwell Server Edition, driver 610.43.02, allocated device
  UUID `GPU-ffcfd284-7534-78f6-3419-81e1769ebf08` (`/dev/nvidia6`).
- A separate `srun --overlap --exact` step used four CPUs and one GPU within
  that allocation. The allocation's existing model and parent job were preserved.
  No GPU computation ran on the Codex host.
- Fresh node-local Sandweave installation, prebuilt runtime 2026.09.11.1,
  Ubuntu 22.04 GNOME image, Xvnc at 1920×1080, 6 GiB guest + 1 GiB runtime.
- VirtualGL 3.1.5, EGL backend `egl0`, OpenGL 4.6 NVIDIA. Added `libxss1`,
  `libxcb-keysyms1` and `mesa-utils` inside the guest.
- No host sudo, KVM or nested Docker was used.

The portable game is mounted read-only at `/opt/stuntrally3`. Launch its binary
through `engine-gpu` and `vglrun`, preserving the staged NVIDIA library path.
The release's shell wrapper overwrites that path. Its `.txz` archive is actually
gzip-compressed; extraction uses automatic detection rather than `tar -xJ`.
URLs and SHA-256 checksums are pinned in `examples/stuntrally3/prepare.sh`.

The example writes `ogre.cfg` and `game.cfg` before launch. It selects OpenGL 3+,
1920×1080 fullscreen and the Beach track, and skips the first-run renderer
dialog. Game configuration version 3303 corresponds to this pinned release.

## Graphics permissions fix

The initial sandbox could run `nvidia-smi`, but VirtualGL failed with
`Could not enable OpenGL API`. The staged EGL and Vulkan JSON files had mode
0640; guest user `ga` could not read them. Making those two non-secret files
readable enabled NVIDIA rendering immediately.

Sandweave 0.2.17 applies 0644 permissions to all three driver descriptors,
including previously cached drivers. Regression tests cover both fresh and
cached staging under umask 077. A fresh sandbox using the built 0.2.17 wheel
and umask 077 verified the permissions, OpenGL renderer, game launch and input
without manual permission repair.

## Capture and performance

The accepted capture used `Recording(fps=30, cursor=False)`:

- 1920×1080; 196.07 seconds of encoded video.
- 5,882 frames captured and encoded; observed cadence 29.99998 captures/second.
- Zero missed capture slots, encoder queue drops or recorder-inserted repeats.
- Segment `418ff3546a184f70a2a5ea9066a703c0`, finalized successfully.

The 12-second gameplay excerpt begins 102.30145 seconds into this segment.
Decoded frames at 1, 6 and 10 seconds were inspected: the car accelerates,
passes track scenery and collides with a rock. Full video, capture timeline,
segment metadata, clip offsets and decoded PNGs are retained under the ignored
`runs/stuntrally3-20260912/` directory.

The game's FPS overlay showed about 66 in an earlier driving check, about
18–23 in decoded frames from the recorded run, and about 28 after recording
stopped in that run. These are observed samples from a shared allocation,
not a controlled frame-time benchmark. The 30 FPS recording cadence does not
imply 30 distinct game frames per second.

The earlier cursor-included recorder timed out during setup and retained only
four captures. Its root cause was not isolated in this game trial. The example
uses the independently verified cursor-free capture path; this change does not
claim to repair cursor-included recording.

This trial does not establish snapshot/resume, audio capture, multiplayer, VR
or compatibility on other GPU models. It is a conventional desktop game demo.
