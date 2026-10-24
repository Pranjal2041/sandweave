# Local JPEG capture and complete HTTP timing

> Released in Sandweave 0.2.46 (October 24, 2026) with these changes: `cursor`
> defaults to `False`; JPEG and PNG capture also work with older desktop helpers,
> which reject only `cursor=True`; and `step(action, format=...)` returns an
> encoded observation. See [encoded screenshots](../docs/desktop.md#encoded-screenshots)
> and [encoded capture release](encoded-capture-release.md). The record below
> describes the original local branch.

September 22, 2026. This is a local-branch extension. It has not been published
or installed into active evaluation workers.

`Desktop.capture_image(format='jpeg', quality=85, cursor=True)` returns encoded
bytes, MIME type, original size, and the native capture metadata. JPEG uses
Pillow's encoder with 4:2:0 subsampling, `optimize=False`, and
`progressive=False`. Encoding occurs after releasing the capture lock.
`format='png'` selects the existing lossless encoder. `capture_png`, action,
and screenshot contracts are retained.

The benchmark HTTP path forwards these bytes without resize or re-encoding and
avoids duplicating the image in its response. It can execute an action and return
the following image in one request. No CLI wrapper is involved in the timing.

On orchard-flame-2, a separate owned benchmark sandbox with a direct local
connection measured HTTP request start through the complete image response:

| Environment | Samples | Mean | Median | p95 |
| --- | ---: | ---: | ---: | ---: |
| Rotating Keyboard, difficulty 5/full/live | 150 | 40.42 ms | 39.20 ms | 52.10 ms |
| LIDAR Blacksite, difficulty 5/full/live, scanning | 150 | 48.15 ms | 46.80 ms | 70.65 ms |

Five warm-ups are retained separately. The target is below 50 ms. Some requests
still exceed it; these are single-client results under existing host load.
The prior benchmark PNG response processing measured 201.40 ms median.

SDK suite: 463 passed, 138 skipped. The actual returned keyboard and LIDAR
images were opened and inspected. LIDAR's pulse counter and visible returns
confirmed a click affected the interface. Existing evaluations were untouched.

Complete report and reproduction:
`/data/user_data/pranjala/weird-cua-bench/docs/fast-http-profile-20260922.md`.
Evidence: `/data/user_data/pranjala/weird-cua-bench/outputs/fast_http_20260922/`.
Private candidate on Flame: `/tmp/wcb-fast-http-20260922`.
