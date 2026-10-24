# Encoded desktop capture and idle connections, 0.2.46

Two client reports, October 24, 2026.

## Keep-alive race

A client's load test read a chat through Weave every 60 s and saw 8 of 31 reads
fail at once with "broken pipe", "connection reset" or "Remote end closed
connection". Reads 50 s or 70 s apart did not fail. Screenshots every 10 s had
no errors.

The controller's aiohttp server closes connections idle for 60 s
(`weave/server.py`, `keepalive_timeout=60`). The synchronous client
(`sandbox/connection.py`) reused idle connections. It already discarded a
connection when the peer's close had arrived, but a close sent at the moment of
reuse races the request. The request then fails without showing whether it was
delivered, so it cannot be retried safely. The async client was unaffected: its
connector retires idle connections after 30 s.

The synchronous client now retires connections idle for 30 s or more before
sending, matching the async client and leaving a 30 s margin under the
controller. Calls closer together keep reusing their connection.

Evidence:

- An aiohttp server with the same keep-alive mechanism, scaled to 1 s, with 40
  calls spaced at exactly the timeout: the previous client failed 4 of 40 with
  `Connection reset by peer`; the new client failed none.
- A real Weave controller at its 60 s timeout, through a loopback relay adding
  25 ms each way, with the next call 59.990 s after each response: the previous
  client failed 5 of 10 with `Remote end closed connection without response`;
  the new client failed none. aiohttp 3.14.5 closes exactly 60 s after the last
  response, so the race needs a call sent after that close but before the close
  crosses the network. Without the added latency, or with calls arriving just
  before the close, neither client failed in 15 calls. The client's network path
  supplied that latency.
- `tests/test_connection_keepalive.py` checks retirement and calls spaced at a
  scaled keep-alive timeout.

## Encoded screenshots

`screenshot()` and `step()` return raw RGB: 6,220,800 bytes per 1920×1080 frame.
A client on a slow link asked for compressed observations. Its benchmark
already used `capture_image` and `capture_png` from the unreleased local branch
recorded in [jpeg-http-capture.md](jpeg-http-capture.md) and
[timestamped-png-capture.md](timestamped-png-capture.md). Those two commits are
now cherry-picked into the release with these changes:

- `cursor` defaults to `False`, matching `screenshot()`.
- JPEG and PNG capture work with any desktop helper. Only `cursor=True`, and the
  guest capture timestamps, require the helper built by 0.2.46 or newer desktop
  setup. An older helper refuses a cursor request with `UnsupportedFeature`
  before the request reaches it, so its channel stays usable.
- `step(action, format="jpeg"|"png", quality=...)` returns the encoded next
  frame in the same request, with `.data`, `.mime_type` and a decoded `.image`.

The client's calls, `capture_image(format="jpeg", quality=..., cursor=True)` and
`capture_png(cursor=True)`, and the fields they read are unchanged.

### Acceptance

Babel `babel-u9-24`, GNOME at 1920×1080, measured through a loopback relay that
limits worker-to-client traffic to 20 Mbit/s:

| Measure | Value |
| --- | --- |
| Raw RGB screenshot | 6,220,800 bytes; 2.52 s median |
| JPEG, quality 85 | 120,683 bytes; 13.2 ms worker encode; 54 ms median capture |
| `step(..., format="jpeg")` | 61 ms median |
| Lossless PNG | 3,226,606 bytes; 119 ms worker encode |

A fresh GNOME installation built the updated helper. Its cursor capture
composited the pointer at the screen center, and its frames carried guest
capture timestamps. The cursor PNG and the quality-85 JPEG were opened and
inspected. An installation prepared before this change returned the same
encoded images, rejected `cursor=True` with `UnsupportedFeature`, and kept
working afterwards. The existing desktop tests (actions, pause/resume,
filesystem restore) and the helper's own tests passed. The host suite passed:
897 passed, 4 skipped.

`tests/integration/test_desktop_capture_live.py` detects the helper and accepts
either result; set `SANDWEAVE_DESKTOP_CURSOR_CAPTURE=1` to require cursor
capture.

## Limits

- Desktop files prepared before 0.2.46 keep their helper. Prepare the `gnome`
  template in new storage for cursor capture and timestamps; setup does not
  replace an existing helper automatically.
- Encoding costs worker CPU: about 13 ms per JPEG and 120 ms per PNG at
  1920×1080. Raw `screenshot()` and `step()` remain the fastest choice on a fast
  local connection.
- Sizes depend on screen content; busy or noisy screens produce larger JPEGs.
