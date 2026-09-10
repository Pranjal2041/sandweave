# Contributing

Install development dependencies with `uv pip install -e '.[test,vr]'`.
Run host tests with `pytest -m 'not integration and not gpu'`.

## Release an SDK version

Update `project.version` in `pyproject.toml` and add its changes to
`CHANGELOG.md`. Commit the changes, then run:

```bash
./deploy
```

Run this on a supported Linux worker with Python 3.11+, `uv`, and `gh` logged
into GitHub. Set `UV_PUBLISH_TOKEN` or `PYPI_API_KEY`, or keep `PYPI_API_KEY`
in the ignored `.env` file. If no token is available and `ut` is installed,
the command requests one through `ut api-key request pypi`.

The command builds only committed files. It runs the host tests, checks the
wheel and source distribution, rebuilds the wheel from the source distribution,
and tests the installed wheel in disposable coding sandboxes. These tests
exercise first-use setup, commands, failures, timeouts, files, pause/resume,
async calls and cleanup. They need internet access and space for a fresh runtime.

After checks pass, it pushes the current branch and version tag, creates a
draft GitHub release, uploads to PyPI, verifies the published file hashes,
then publishes the GitHub release. The SDK keeps its pinned runtime binaries;
an SDK release does not rebuild or republish the engine.

To run the same checks without pushing or publishing:

```bash
./deploy --check
```

Artifacts, logs and a validation receipt stay in `runs/deploy/`. Repeating
`./deploy` for the same commit reuses the verified artifacts and resumes a
partial publication. Existing remote files must match their SHA-256 hashes;
the command never overwrites them. A changed commit requires new validation.
Use `./deploy --check --recheck` to repeat validation for the same commit.
Fresh runtime storage uses the system temporary directory (`TMPDIR` if set),
which must be outside this checkout. Its path is printed and retained for
inspection; only the disposable sandboxes and their idle worker are stopped.

The default checks cover the SDK and coding runtime. Changes to desktop, VR,
GPU or snapshot behavior also need the relevant live acceptance tests before
release; `./deploy` does not certify those workloads.
