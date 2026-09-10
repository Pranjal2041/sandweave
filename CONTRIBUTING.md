# Contributing

Install development dependencies with `uv pip install -e '.[test,vr]'`.
Run host tests with `pytest -m 'not integration and not gpu'`.

## Documentation

Public guides live in `docs/`; navigation and site settings are in `mkdocs.yml`.
The site uses Material for MkDocs with pinned build dependencies. To preview:

```bash
uvx --with-requirements docs/requirements.txt mkdocs serve
```

Build and check internal links and Python example syntax:

```bash
uvx --with-requirements docs/requirements.txt mkdocs build --strict
python scripts/check-docs.py
```

Add `--browser` to the check command in an environment with Playwright and
Chromium installed. It checks search, navigation, code copying, theme switching,
installation tabs, and mobile navigation. Screenshots and the receipt are saved
under `runs/docs-acceptance/`. No sandbox or cluster is launched.
Pass `--url https://pranjal2041.github.io/sandweave/` with `--browser` to
exercise the published site instead of the local build.

After committing and pushing the reviewed source to `main`, publish it with:

```bash
uvx --with-requirements docs/requirements.txt mkdocs gh-deploy --strict
```

This updates the generated `gh-pages` branch. GitHub Pages serves
`https://pranjal2041.github.io/sandweave/` from that branch. Generated site files
stay out of the source branch. Documentation publication does not require a
new runtime build or SDK release.

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
async calls and cleanup. The installed build-transfer path is also checked
with private, inherited-group and default-ACL directories where the host
supports those layouts, including interrupted and unsuccessful builds.
These checks need internet access and space for a fresh runtime.

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
