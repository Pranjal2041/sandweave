# Five random Energy50 setup checks

All five selected tasks completed setup, initial screenshot capture and a
canonical verifier call. Each screenshot was visually inspected against the
task's original setup configuration. All five disposable sandboxes finished
cleanup. No SDK, template, engine or private reference changes were needed.

The run used `0.2.20rc2` source and runtime `2026.09.14.2`, with one concurrent
task, four vCPUs, 16 GiB guest memory, 1 GiB runtime memory and no GPU. It reused
the prepared OSWorld filesystem baseline from the [service acceptance](osworld-services.md).
Each task received a separate sandbox. Launch records confirmed the released
engine and the OSWorld console, netlink and sysctl options.

Five tasks were sampled without replacement from all 50 using the randomly
generated seed `10159986225375561053`. No task was substituted or retried.
The run executed them in benchmark order.

| Task | Setup time¹ | Screenshot review |
| --- | ---: | --- |
| GIMP vignette, `a746add2` | 5.24 s | [Correct dog image named in its color-profile prompt](assets/osworld-random5/gimp.png). |
| Calc unique names, `abed40dc` | 12.46 s | [Original duplicate names and unfinished output column](assets/osworld-random5/names.png). |
| Calc movie titles, `a9f325aa` | 13.14 s | [Original irregular spacing and capitalization](assets/osworld-random5/movie-titles.png). |
| Calc acceleration, `4de54231` | 14.11 s | [Mass values and the supplied row-2 acceleration values](assets/osworld-random5/acceleration.png). |
| Impress save slides, `a097acff` | 13.81 s | [Correct presentation open on slide 1 of 17](assets/osworld-random5/slides.png). |

¹ Task setup after sandbox acquisition; excludes pool preparation and desktop
startup. The four later acquisitions plus setup took 53–57 seconds. The first
task used the already preloaded sandbox. These timings do not measure fleet
throughput.

All screenshots are 1920×1080. Setup logs confirm the requested downloads,
application commands and matching visible windows. The setup, preparation
directory creation and cleanup commands all exited zero for every task.
All five untouched tasks scored zero; the verifier calls completed successfully,
but no agent attempted the requested work.

GIMP's original color-profile question is visible and was left unchanged. The
application launcher logged a missing `canberra-gtk-module`; LibreOffice logged
a `javaldx` warning. Neither prevented the observed file opening or evaluation.
The movie-title screenshot also retains LibreOffice's contribution banners.
The retained baseline build log contains an earlier ignored UFW/iptables trigger
error and ends with `DELTA_APPLIED_OK`; this run reused that baseline and did
not rerun its image build. This check concerns these five task setups, not all
Java, sound or firewall features of the image.

The [machine-readable record](osworld-random5-summary.json) contains full task
IDs, the sampling seed, runtime pins, window evidence, scores, cleanup checks
and evidence hashes. Raw setup stdout/stderr, exit codes, launcher logs and
screenshots are retained locally under the ignored
`runs/osworld-acceptance/20260914-random5` directory.

Repeat the exact selection with the acceptance script:

```bash
python scripts/accept-osworld-benchmark.py \
  --source /path/to/unchanged/cua-speed-run \
  --output /path/to/new-results \
  --sample 5 --seed 10159986225375561053
```

Omit `--seed` for a fresh random selection. An existing prepared baseline can
be reused with `--cache`; this run used `--cache osworld-services`.
The audit script records setup output through a forwarding wrapper; the
original setup hook, task configuration and verifier remain unchanged.
