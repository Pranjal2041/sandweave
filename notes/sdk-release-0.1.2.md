# SDK 0.1.2 acceptance

Released from `c2a2e1ce8cb952edc1075af84a43213ba47fdfcf` with `./deploy`.
The command pushed `main` and `v0.1.2`, uploaded the wheel and source
distribution, and verified their hashes on both services:

- [PyPI 0.1.2](https://pypi.org/project/sandweave/0.1.2/)
- [GitHub release](https://github.com/Pranjal2041/sandweave/releases/tag/v0.1.2)

`run` and `run.aio` now return failed command results by default. `check=True`
retains the previous exception behavior. Setup acceptance commands explicitly
require success. The runtime pin remains `2026.09.09.1`; no engine code changed.

## Validation

`./deploy --check` built the committed source outside the checkout. All 155 host
tests passed; 65 integration/GPU cases were excluded from that host-only run.
Both distributions passed `twine check --strict`. Rebuilding the source
distribution produced identical uncompressed wheel contents. The installed
package reported `sandweave.__version__ == "0.1.2"`.

The three cases in `tests/integration/test_sdk.py` then passed against that
installed wheel on Babel with Python 3.13.0, in 60.75 seconds including automatic
installation into empty storage. Setup fetched the pinned GitHub binaries and
prepared the coding template. The cases covered:

- Successful commands, single-string shell syntax, literal argv, files and streams.
- Failed commands returning separate stdout/stderr and a nonzero exit code.
- The user's division-by-zero example returning `ZeroDivisionError` in stderr.
- Explicit `check=True` preserving the failed result on `CommandError`.
- Execution timeouts and output limits raising for synchronous and async calls.
- CLI output forwarding and exit status 7 without an SDK exception traceback.
- Setup scripts, borrowed handles, pause/resume and context cleanup.

An earlier validation stopped before publication because the new CLI test put
`--no-stdin` after the sandbox ID. That position belongs to the guest command.
The corrected test supplies the option before the ID; both complete live runs
reported below use the corrected test.

After publication, a new Python 3.12.9 environment on Orchard Flame installed
`sandweave[test,vr]==0.1.2` from public PyPI. The same three cases passed in
48.37 seconds. That run copied prepared runtime files into separate test
storage; it did not rebuild the engine or alter the user's installation.

The changed command examples were opened and visually inspected in the actual
GitHub and PyPI README pages. Both show the division-by-zero result example,
the optional `check=True`, and the default failure behavior correctly.
The PyPI page's version heading and install command show 0.1.2. The external
Shields badge was still cached at 0.1.0 during inspection.

## Artifacts

| File | SHA-256 |
| --- | --- |
| `sandweave-0.1.2-py3-none-any.whl` | `38963458a762907887542e8d1d8c5fc1fa73b76621fd169f983c516f81bd6553` |
| `sandweave-0.1.2.tar.gz` | `62b38cea7a9320714c5b48eb5298f96993d9ca7d02f034fe2af910c6ff0a498c` |

Local build artifacts, validation/publication receipts, terminal logs and
rendered screenshots are retained under
`runs/deploy/0.1.2-c2a2e1ce8cb9/`. Babel's validation workspace is
`/tmp/sandweave-deploy-pyr14yui`; Flame's is
`/tmp/sandweave-pypi-012-sr0i5jzf`. Temporary storage is diagnostic only;
the committed source and public release files are the durable release record.

This SDK release does not change desktop, VR, GPU or snapshot implementation.
Those workloads retain their earlier acceptance results; they were not rerun
for this command-result default change.
