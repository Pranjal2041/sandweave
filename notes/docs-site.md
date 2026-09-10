# Public documentation site

The public site is <https://pranjal2041.github.io/sandweave/>. Its source is
`docs/` and `mkdocs.yml`; contributor commands are in `CONTRIBUTING.md`.

The navigation follows the task-oriented guides and short examples in
[Miles](https://miles.radixark.com/docs),
[Prime-RL](https://docs.primeintellect.ai/prime-rl/overview), and
[OpenHands](https://docs.openhands.dev/overview/introduction). Sandweave uses
Material for MkDocs, with pinned dependencies, local search, system fonts,
light/dark themes, copy buttons, and mobile navigation. The build is static and
does not require a documentation service account.

## Content and contract

The 20 pages cover installation, commands, files, resource settings, networking,
cleanup, saved state, async Python, desktops, VR, custom templates, clusters,
pools, jobs, the dashboard, and Python/CLI references. The existing README
examples remain the public contract. Examples were compared with the current
SDK definitions, command parser, and detailed implementation notes.

The README badge and existing SDK notes link to the site. The package's
Documentation metadata points there for its next release; the already
published 0.2.3 package is unchanged. Its existing documentation link still
reaches `notes/sdk-usage.md`, which now links to this site.

## Acceptance

Local acceptance on 2026-09-10:

- `mkdocs build --strict` completed without link or configuration warnings.
- `scripts/check-docs.py` parsed 71 Python examples and resolved 1,343 internal
  links and anchors across 21 HTML pages, including the generated 404 page.
- Chromium exercised clipboard contents, search results, desktop navigation,
  installation tabs, theme switching, and mobile navigation between sections.
- Desktop and mobile screenshots were opened and inspected. Mobile checks
  included absence of page-level horizontal overflow; code remains scrollable.
- Browser checks reported no JavaScript exceptions or unsuccessful HTTP responses.

Screenshots and the JSON receipt are in the ignored `runs/docs-acceptance/`.
The checks compile example syntax; they do not execute sandbox workloads. No
existing sandbox, desktop, worker, or controller was changed by this task.

## Publication

After source review and commit, `mkdocs gh-deploy --strict` publishes generated
files to `gh-pages`. GitHub Pages serves the branch root. This is an explicit
docs deployment; pushing `main` alone does not rebuild the site. The publish
command is documented in `CONTRIBUTING.md` and does not use a Git worktree.

Use `scripts/check-docs.py --browser --url https://pranjal2041.github.io/sandweave/`
to repeat the browser checks against the public deployment. Save a separate
receipt with `--output runs/docs-public-acceptance`.
