# Offline projection and what the eye recordings establish

The saved GunSpinning left/right videos can be reprojected offline into partial
equirectangular images. They cannot supply a complete 180-degree or 360-degree
scene at each instant. Both eyes look forward from slightly different positions;
they do not point in opposite directions. Unseen directions remain black.

The September 8, 2026 conversion used local files and CPU FFmpeg only. It did not
start, connect to, or change a sandbox, game, runtime, or Slurm allocation.

## Capture provenance

These are separate compositor eye views for the configured **virtual headset**,
with the recorded scripted head/controller trajectory. They are not a
through-the-lens recording or a validation of physical headset delivery.

The source audit used the saved Monado staging tree at
`tools/gpu/vr/monado-source`, upstream revision
`f8dfadfeaeb46df3eec17bd76b7abdf42a79108c` plus the lab's committed
[Monado patch](monado-vr-lab.patch):

- `src/xrt/compositor/main/comp_renderer.c`, `comp_renderer_draw`: disables
  the direct projection fast path during lab capture, takes eye 0 and eye 1
  scratch images from the same compositor frame state, and passes both image
  views to `comp_mirror_do_blit` with the full normalized rectangle.
- `src/xrt/compositor/main/comp_mirror_to_debug_gui.c`: resizes each image into
  its own half of the packed output, then transfers/publishes the complete pair.
- `scripts/vr_stream.py`, `Frame.image`: splits that packed image into the two
  eye videos. It does not synthesize a second eye from a mono screenshot.
- Scratch composition uses the eye FOV (`comp_high_level_render.h` and
  `comp_render.h`); capture precedes the final lens-distortion target.
  Compositor timewarp can already be present, so these are not necessarily
  byte-for-byte copies of the game's submitted textures.

The [original video manifest](gunspinning-vr-video.json) records hashes, paired
timestamps and the earlier visual checks. The images show different perspectives
on the hands, guns and range. This supports stereo rendering; it does not prove
physical headset optics, scale, comfort, scanout timing, or correct perception
for an individual viewer's eye spacing. In a headset, each eye receives its
own view; a flat side-by-side preview is not that binocular viewing experience.

## Projection calibration and corner masks

`src/xrt/drivers/remote/r_hmd.c` specifies an 85-degree horizontal FOV for each
eye, a 0.13-by-0.07-metre display model split horizontally, and centered lenses.
`u_device_setup_split_side_by_side` and `math_compute_fovs` therefore imply:

```text
horizontal FOV = 85 degrees
vertical FOV   = 2 atan((0.07 / 0.065) tan(85 degrees / 2))
               = 89.239738 degrees
```

`comp_renderer.c` selects `COMP_TARGET_FOV_SOURCE_DISTORTION`, so those device
FOVs drive the composed scratch views. The remote HMD calls
`u_distortion_mesh_set_none`; it is not modeling a specific headset's lenses.
The compositor's default eye separation is 0.063 metres, distinct from the
0.065-metre lens separation in the simple display model.

**Calibration is derived from saved source/configuration, not measured from
the MP4s.** The recording does not contain per-frame FOV telemetry. Conversion
assumes the recorded build used these defaults. Arbitrary left/right videos
alone do not uniquely identify their camera projection or FOV.

The corner cutouts are present in the originals. Monado's default visibility
mask clips four corner triangles into an octagonal visible region
(`u_visibility_mask.c`); the remote HMD uses that default, and xrizer v0.5
`GetHiddenAreaMesh` exposes the OpenXR mask to OpenVR. The observed cutout shape
is consistent with this source path. This was a source audit, not a new runtime
trace proving each corner's clear color. Conversion retains those pixels.

## Offline conversion

The [converter](../scripts/vr-eyes-to-equirect.py) accepts separate videos and
explicit symmetric rectilinear FOVs. It refuses unequal/nonmonotonic timestamps
and existing output directories, leaves originals untouched, exports both eyes
and a synchronized side-by-side preview, and checks every output presentation
timestamp against the input. FFmpeg's `v360` visibility alpha is premultiplied
onto black so sampling outside the captured frustum cannot repeat edge pixels
as if they were observed scenery.

```bash
python scripts/vr-eyes-to-equirect.py \
  runs/gunspinning/gunspinning-stereo-demo-01/frames/left-eye.mp4 \
  runs/gunspinning/gunspinning-stereo-demo-01/frames/right-eye.mp4 \
  --output runs/gunspinning/gunspinning-stereo-demo-01/partial-equirectangular \
  --hfov 85 --vfov 89.23973803804061 \
  --fov-provenance 'Saved Monado remote-HMD defaults; no per-frame FOV telemetry'
```

Each eye has a 360-by-180-degree canvas at 2048-by-1024 pixels, with only the
captured forward region populated. This resamples scene detail; the originals
remain the higher-detail evidence. The side-by-side preview is 4096-by-1024.
The projection follows the recorded eye direction in each frame: recorded
head motion stays in the video, with no world alignment or stabilization.
No scene reconstruction, depth generation, or new viewpoint is performed.
Spherical/stereo player metadata is not injected, so a VR player would require
manual projection/layout selection; physical playback was not tested.
Audio is omitted.

The operation follows FFmpeg's documented
[v360 filter](https://ffmpeg.org/ffmpeg-filters.html#v360), including rectilinear
input and explicit input FOVs. OpenXR defines individual view-frustum angles in
[XrFovf](https://registry.khronos.org/OpenXR/specs/1.1/man/html/XrFovf.html).

## Saved outputs and acceptance

- [Left eye](../runs/gunspinning/gunspinning-stereo-demo-01/partial-equirectangular/left-eye.mp4)
- [Right eye](../runs/gunspinning/gunspinning-stereo-demo-01/partial-equirectangular/right-eye.mp4)
- [Synchronized preview](../runs/gunspinning/gunspinning-stereo-demo-01/partial-equirectangular/stereo-preview.mp4)
- [Conversion manifest](../runs/gunspinning/gunspinning-stereo-demo-01/partial-equirectangular/manifest.json)

All three outputs contain 748 frames over 33.668 seconds, with presentation
timestamps exactly matching both source eye videos. All 2244 output frames
decoded without errors. Source capture cadence remains 22.19 stereo pairs/s;
this conversion makes no new application-FPS measurement. Generated videos
and verification artifacts remain ignored under `runs/`. Original video
hashes remain unchanged.

Decoded left/right frames at 21 seconds and the synchronized preview at 27
seconds were opened and visually inspected: the range, hands, guns and stereo
perspectives remain, the later hands/view move, and unseen directions are
black. Pixel checks on the distant outer canvas columns returned RGB zero.
These are frame inspections plus a full decode, not headset/video-player
playback validation. The [acceptance report](vr-offline-equirectangular.json)
preserves output hashes, timestamps, inspected frames and audited source hashes.
