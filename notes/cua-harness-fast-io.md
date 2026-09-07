# cua-auto-harness audit of Xvnc fast I/O

This audit uses the standalone lab's `FastIOClient` input and screenshot APIs,
with patched gVisor systrap on `babel-u5-28`. It does not use Gym Anything's QEMU
runner, KVM, host sudo, or a model. The user's Resolve desktops are separate
from the disposable audit environment.

## Full result, 2026-09-07

**94 passed, 1 failed, 0 flaky, 0 harness errors, 5 unsupported.** The complete
100-entry catalog was selected; its 95 supported entries ran five times each:
**470 passing repetitions and 5 failing repetitions**. All failures belong to
`type_emoji_zwj`. No input/screenshot implementation or upstream oracle was
changed to obtain this result.

| Catalog group | Passed | Failed | Unsupported |
|---|---:|---:|---:|
| Clicks, moves, button holds | 16 | 0 | 2 |
| Drags | 11 | 0 | 0 |
| Keys and chords | 34 | 0 | 0 |
| Scrolling | 9 | 0 | 3 |
| Mixed sequences | 8 | 0 | 0 |
| Typing | 16 | 1 | 0 |
| **Total** | **94** | **1** | **5** |

This includes five clean repetitions each of double/triple clicks, five-click
bursts, modifier drags, 25-tick scroll bursts, Caps Lock, shell-sensitive text,
multiline/tabs, accents/CJK, RTL, combining marks, and 500-character text.
Typing after the rejected Unicode action continued to work.

Screenshot evidence was audited separately from action totals. **450/450
application-observable captures decoded to the probe's final recorded state**;
none were stale or undecodable. Twenty desktop-shortcut repetitions were
raw-observer-only, and five actuator failures returned before capture. All
five multipoint-drag repetitions used the harness's raw motion-density
corroboration: Tk coalesced intermediate movement, while XRecord saw all 32
held-button movements and the correct endpoint.

The main audit took about 36 minutes including desktop boot and cleanup. Its
quiescence/reset/evidence work dominates that duration; this is not an input
or screenshot latency result. This full catalog run used a CPU desktop. The
earlier CPU/GPU rendering and latency acceptance remains documented separately.

The unmodified upstream [Markdown report](cua-harness-evidence/full-cpu/report.md)
and [JSON report](cua-harness-evidence/full-cpu/report.json) are committed along
with [all per-repetition verdicts](cua-harness-evidence/full-cpu/verdicts.json),
[channel statistics](cua-harness-evidence/full-cpu/channel-summary.json),
[failure evidence](cua-harness-evidence/full-cpu/failure-evidence.json), and
[versions/build provenance](cua-harness-evidence/provenance.json).
The complete PNG/app/raw-event bundles remain in
`runs/cua-harness-full-cpu/`, deliberately ignored by Git. Representative click,
Unicode and final long-text screenshots were opened and inspected. All audit
desktops were discarded; `resolve-gpu2` and `resolve-optfix` remained running.

## Unicode attribution

The same seven keypad/Unicode entries were exercised five times through each
injector in separate fresh environments:

| Subset | Consistent passes | Consistent failures | Flaky | Harness errors |
|---|---:|---:|---:|---:|
| Fast I/O, seven cases in catalog order | 6 | 1 | 0 | 0 |
| xdotool reference, same seven cases | 3 | 0 | 4 | 0 |
| Fast I/O, joined emoji alone on a fresh connection | 1 | 0 | 0 | 0 |

The fast-I/O failure is `type_emoji_zwj`, rejected on all five repetitions with
`no unused keycode for text`. The helper keeps additional keysyms mapped for
its entire connection lifetime. The desktop had **19 unused keycodes** before
Unicode typing. The preceding BMP, emoji, RTL and combining-mark strings need
18 distinct extra mappings; the joined-emoji string needs another six. Only
one free slot remains, so validation rejects the whole action before injection.
This is a fast-I/O allocation limit that affects ordinary sequences of short
Unicode strings, not just one oversized request. It is not full Unicode
conformance. Closing/reopening fast I/O resets the mapping pool, but the audit
does not insert that workaround between entries.

On its own, the exact string `hi 👨‍👩‍👧 ok 👋🏽 end` passes five out of five times.
This distinguishes exhaustion across a session from inability to inject these
particular codepoints. The public input implementation is unchanged during
the audit. Recycling keycodes prematurely could introduce the keymap-refresh
race seen on the reference path; a fix requires its own validation.
Inspected screenshots show fallback boxes for some CJK/emoji glyphs with this
image's fonts. The Unicode assertions compare the actual text codepoints;
they do not establish font or grapheme rendering support.

The reference run had six failed repetitions across four flaky entries:
BMP accents (2/5), rocket emoji (1/5), RTL (2/5), and decomposed combining
marks (1/5). For example, an expected `ï` in `naïve` arrived at the probe as
`keysym="??", char=""`, producing `nave`. These are retained as reference
failures; they do not excuse or change the fast-I/O verdict. The five joined-
emoji reference repetitions all passed.

Unmodified reports and failure traces:
[fast-I/O subset](cua-harness-evidence/cold-text/report.md),
[reference subset](cua-harness-evidence/reference-text/report.md), and
[isolated joined emoji](cua-harness-evidence/emoji-isolated/report.md).
The [pre-Unicode keymap](cua-harness-evidence/keymap-before-typing.json) was read
without changing input state in the main audit desktop.

## Reproduce

The harness is pinned to
[`gym-anything/cua-auto-harness` commit `fc07c2d3a10e56c0fc0578280df9563af6f49d1f`](https://github.com/gym-anything/cua-auto-harness/tree/fc07c2d3a10e56c0fc0578280df9563af6f49d1f),
package version **0.4.12**. It contains **100 canonical runner interactions**,
each with five default repetitions. Neither optional nor known-hard entries
are excluded. Access to the private repository uses the configured GitHub CLI.

From the existing, staged lab:

```bash
cd ~/scratch/general-vm
mkdir -p downloads/cua-auto-harness-pinned
gh api repos/gym-anything/cua-auto-harness/tarball/fc07c2d3a10e56c0fc0578280df9563af6f49d1f \
  > downloads/cua-auto-harness-pinned.tar.gz
tar -xf downloads/cua-auto-harness-pinned.tar.gz \
  -C downloads/cua-auto-harness-pinned --strip-components=1
python -m venv --system-site-packages tools/cua-harness-venv
tools/cua-harness-venv/bin/python -m pip install downloads/cua-auto-harness-pinned
PYTHONUNBUFFERED=1 tools/cua-harness-venv/bin/autoharness test \
  --adapter scripts/cua_harness_adapter.py:GVisorXvncAdapter \
  --env-dir . -O name=fastio-harness-audit \
  --no-tui --out runs/cua-harness-audit
```

Choose a fresh environment name and output directory for each run. The adapter
only accepts disposable names beginning with `fastio-harness-`. It boots an
8-GiB, four-advertised-CPU desktop and waits for GNOME session readiness,
1920×1080 resolution and desktop painting before allowing probe deployment.
The canonical corner coordinates require this resolution. Tk, python-xlib,
xdotool, xset and curl are installed inside this private guest if missing.

The adapter stops and discards its environment on normal completion. `-O
keep=true` keeps a disposable environment for investigation; clean it explicitly
with `python scripts/env.py stop NAME --discard`. If the harness fails during
its own probe deployment, inspect `env.py list --active` and clean the disposable
environment. The upstream driver calls setup before its teardown-protected block.

Use `--entries click_left_center,drag_short,key_tab,type_plain --reps 1` for a
small deployment smoke check. Use `--runner xdotool-reference` for targeted
attribution with the upstream reference command builder. This reference option
must never be confused with a fast I/O result. `-O gpu=0` exposes an allocated
GPU; the harness's Tk probe itself does not exercise GPU application rendering.

## What is measured

Every tested input is translated to public mouse/keyboard dictionaries and
sent through `FastIOClient.action`. Screenshots use fresh
`FastIOClient.screenshot` images, then PNG encoding for the harness's evidence
bundle. The adapter does not invoke the private XTEST protocol directly.

Modifiers wrap gestures with public key-down/key-up actions in one ordered
batch. Middle-button dragging uses public button states and move actions.
Canonical key names are translated to X keysyms accepted by the public API.
The existing durationless drag API is exercised as-is: the adapter does not
add artificial inter-motion sleeps. Explicit canonical `Wait` actions retain
their specified delays.

The unmodified upstream harness deploys both its Tk application observer and
raw XRecord observer. It uses xdotool to reset modifier/button state and focus
the probe **between repetitions**, independently of the tested injector.
Application event records establish received input; screenshot HUD decoding
checks whether capture reflects a recorded state from this repetition. The
upstream visual oracle also passes an older recorded state, noting the lag in
its detail, and calls an undecodable/occluded strip `unavailable`. Therefore an
action pass alone does not establish final-state screenshot freshness; visual
details must be inspected separately. The raw observer can
judge WM-consumed shortcuts and distinguish X-server drag delivery from
toolkit motion coalescing. Unicode text is judged by application text, because
the raw observer only reconstructs ASCII.

The harness waits for input quiescence and HUD painting before its screenshot.
These waits make it a correctness audit, not a fast-I/O latency benchmark or a
test of whether an immediate action acknowledgment guarantees application paint.
The [separate latency and CPU/GPU acceptance](xvnc-fast-io.md) covers those
timing distinctions. A Tk audit does not qualify all Firefox/Earth/Resolve
interactions or every keyboard layout.

The current public API lacks back/forward mouse buttons and horizontal scroll.
Consequently `click_button_back`, `click_button_forward`, `scroll_left_3`,
`scroll_right_3` and `scroll_diagonal` are reported as `not_measurable`, not as
passes. The harness's `totals.entries` excludes these unsupported entries:
**95 exercised entries + 5 unsupported entries = 100 catalog entries**.

## Evidence format

The upstream output contains `report.md` and `report.json`, plus
`entries/ENTRY/entry.json` and one directory per repetition with `verdict.json`,
`events.json`, `raw_events.json` and a PNG screenshot. An actuator exception
produces a verdict without post-action evidence. Preserve the original verdicts
when doing reference comparisons or follow-up tests.

The early deployment smoke runs exposed GNOME startup races: Xvnc could accept
input while the eventual desktop/probe still lacked focus. Those artifacts are
retained under `runs/cua-harness-smoke*`. The final readiness check passed all
four smoke cases before the complete audit began. Failed startup attempts are
not silently counted as successful conformance repetitions.
