# Fast I/O action repairs

The standalone Xvnc backend now exposes back/forward mouse buttons and
horizontal/diagonal scrolling through its public API. The harness adapter only
translates its canonical actions to that API. The upstream catalog, probe,
oracles, reset behavior and settling waits remain unchanged.

```python
desktop.action({'mouse': {'back_click': [500,400]}})
desktop.action({'mouse': {'forward_click': [500,400]}})
desktop.action({'mouse': {'scroll': {'dx': -3}}})
desktop.action({'mouse': {'scroll': {'dx': 3, 'dy': -2}}})
```

Integer scrolling is backward compatible. The five previously unsupported
canonical entries are `click_button_back`, `click_button_forward`,
`scroll_left_3`, `scroll_right_3` and `scroll_diagonal`.

## Full canonical rerun

**100 passed, 0 failed, 0 flaky, 0 errors, 0 unsupported.** All 100 entries ran
their default five repetitions in one fresh CPU environment, through one
persistent helper session: **500/500 repetitions passed**. The original
`type_emoji_zwj` failure and each of the five newly supported mouse cases passed
all five repetitions without resetting the helper or changing the upstream test.

All **480 application-observable screenshots** decoded to the probe's final
recorded state. The remaining 20 desktop-shortcut repetitions use the raw
observer. None of the canonical captures was stale or undecodable. Representative
back-button, horizontal-scroll, joined-emoji and 500-character screenshots were
opened and inspected. The extreme stress-test capture limits below are separate
from this canonical result.

The unmodified [Markdown report](cua-harness-evidence/fixes/full-cpu/report.md),
[JSON report](cua-harness-evidence/fixes/full-cpu/report.json),
[all 500 verdicts](cua-harness-evidence/fixes/full-cpu/verdicts.json) and
[channel summary](cua-harness-evidence/fixes/full-cpu/channel-summary.json)
are committed. The complete evidence bundle is in `runs/cua-harness-fixed-full/`.
All 45 host unit tests and the compiled C allocator check also passed.
The disposable environments were discarded; the two existing Resolve desktops
remained running with their original process identities.

## Unicode repair

The original failure was allocation exhaustion: earlier Unicode entries used
18 of the desktop's 19 spare X11 keycodes before `type_emoji_zwj` requested six
more. Closing the helper between cases would hide that bug. Instead, it now
reuses helper-owned mappings while reserving held keys and every key needed
throughout the current batch.

Reuse also needs synchronization. A simple LRU cache fixed the short canonical
sequence but corrupted a rapid stream of 1,024 distinct CJK characters. In the
first stress repetition, 704 characters changed because old queued key events
were interpreted using newer mappings. An XTEST server acknowledgment does not
prove that a client has translated its input.

The repair tracks receiving managed windows and uses `_NET_WM_PING` around
mapping changes. It first drains previous recipients before eviction, then
allows the current recipient to process mapping notifications before new input.
The second step matters: an intermediate implementation substituted two old
characters in one of three stress repetitions.

This is a tested toolkit event-loop synchronization technique, not an X11
guarantee for arbitrary applications. The
[EWMH ping protocol](https://specifications.freedesktop.org/wm-spec/latest/ar01s06.html)
specifies a client response; the
[Tk implementation](https://github.com/tcltk/tk/blob/core-8-6-branch/unix/tkUnixWm.c)
handles it internally. Paint completion remains separate. Unicode requiring new
mappings rejects an unsupported or stalled recipient before emitting input;
the three-second timeout leaves the action retryable. A single batch must still
fit the available keycodes. Raw observers, independent input-method daemons,
keyboard grabs and concurrent focus changes within a batch are outside this
qualification. See [API details and limits](xvnc-fast-io.md).

Detachment uses a recoverable preflight. Held temporary keys must be released,
and pending recipients must respond before mappings and SHM are detached.
Normal held modifiers retain their previous pause/resume behavior.

## Additional live acceptance

| Check | Result |
|---|---|
| Unmodified Tk harness, 1,024 distinct CJK characters in rapid batches | Exact text in all three repetitions |
| GTK entry, same 1,024-character turnover | Exact text |
| Previous GTK recipient stopped after focus moves to another window | New input rejected after 3.02 seconds; retry after resuming succeeds |
| Oversized batch, preceded by otherwise valid input | Entire batch rejected without typing the valid prefix |
| Held temporary key during detach | Rejection preserves the helper; release and retry succeeds |
| CPU pause/resume and live restore | Application text and nonce preserved; further Unicode input accepted |
| GPU-enabled Firefox address bar | Exact 1,024-character text after focus was established |

The GTK turnover actions each contain 16 new codepoints; their median was
34.04 ms and p95 was 66.67 ms, including mapping installation and client
synchronization. This is a cold Unicode mapping workload, not the warm mouse
or screenshot benchmark. Its 64 chunks are submitted without inter-action waits.

The Firefox test uses a fresh profile and the existing VirtualGL launch path.
Its WebGL page reported `NVIDIA GeForce 8800 GTX, or similar`; that literal
browser-reported string is preserved in the evidence rather than treating it
as the physical GPU model. The address bar is focused and verified before the
stress starts. Clipboard reads only observe the final value; input uses XTEST.

Application text and pixels must be assessed separately. The extreme Tk stress
produced old HUD captures even when all text matched; two were unavailable to
the visual oracle because they showed a previous repetition. The immediate
GTK and Firefox captures also preceded repaint; the restored GTK screenshot
shows the later state. These tests establish Unicode delivery and lifecycle
behavior, not a new action-to-painted-frame latency guarantee. Fonts in this
image display fallback boxes for many CJK/emoji characters.

Committed evidence: [Tk stress report](cua-harness-evidence/fixes/unicode-turnover/report.md),
[per-repetition verdicts](cua-harness-evidence/fixes/unicode-turnover/verdicts.json),
[repair diagnostics](cua-harness-evidence/fixes/unicode-repair-diagnostics.json),
[GTK/lifecycle report](cua-harness-evidence/fixes/gtk-keymap.json),
[Firefox report](cua-harness-evidence/fixes/firefox-keymap.json), and
[source/build provenance](cua-harness-evidence/fix-provenance.json).
Full screenshots and event logs remain under the named, ignored `runs/` directories.

## Reproduction

Build the helper and run the focused host checks:

```bash
scripts/build-fast-io.sh
python scripts/test-fast-io.py
gcc -std=c11 -O2 -Wall -Wextra -Werror sources/test-fast-io-keymap.c \
  -o tools/fast-io/test-keymap -lxcb -lxcb-shm -lxcb-xtest
tools/fast-io/test-keymap
```

With the [pinned harness installation](cua-harness-fast-io.md#reproduce), use
fresh disposable names for live tests:

```bash
tools/cua-harness-venv/bin/python scripts/test-fast-io-unicode.py fastio-harness-unicode
tools/cua-harness-venv/bin/python scripts/test-fast-io-keymap-live.py fastio-harness-keymap
tools/cua-harness-venv/bin/python scripts/test-fast-io-firefox-unicode.py fastio-harness-firefox --gpu 0
tools/cua-harness-venv/bin/autoharness test \
  --adapter scripts/cua_harness_adapter.py:GVisorXvncAdapter --env-dir . \
  -O name=fastio-harness-fixed --no-tui --out runs/cua-harness-fixed
```

The first script sends 64 sixteen-character text actions consecutively through
the public API, three repetitions, without adapter sleeps or intermediate probe
reads. Its application text and screenshot verdicts are distinct evidence.
The GTK regression tests the same turnover, whole-batch rejection, a stopped
previous recipient after focus moves, retry, held-key detach, pause/resume and
live restore. The final command selects all 100 canonical entries with their
default five repetitions in one environment and one persistent helper session.

Existing services retain their loaded helper. `python scripts/fastio.py close
ENV` closes only that environment's I/O service; the next I/O call installs the
current helper. The user's running Resolve desktops are preserved during these
disposable tests.
