# Standalone no-KVM lab

Work here is independent of Gym Anything's main repository. Runtime experiments
use the existing Slurm allocation without host sudo or KVM. Preserve user-facing
desktops when testing; use separate disposable environments for new tests.

## Git hygiene

The user expects every completed change to be committed, with generated or local
artifacts deliberately covered by `.gitignore`. Apply this habit on every task.

- Work on a branch in the existing checkout; never create Git worktrees.
- Inspect Git status before editing and again before finishing. Preserve unrelated
  user changes. Stage the files belonging to the task explicitly and review the
  staged diff before committing.
- Commit lab scripts, tests, documentation, configurations, patches and source
  probes in this repository. Keep downloaded dependencies, binaries, images,
  snapshots, recovery archives, logs and local credentials ignored. Do not hide
  authored implementation changes with new ignore rules merely to clean status.
- `sources/gvisor` is an independent Git checkout, ignored by this repository.
  Commit engine changes there, then update `notes/source-revisions.json` and
  `notes/gvisor-no-kvm-prototype.patch` here to match the committed engine.
- At handoff, check both this repository and `sources/gvisor` for uncommitted or
  untracked files. Report the relevant commit IDs and any unresolved changes.
- A recovery archive supplements Git history; it does not replace a commit.
  Push only when the user requests it.

## Validation

Read `README.md`, `notes/gvisor-lab-reproduction.md` and the relevant implementation
notes before changing runtime behavior. Run checks appropriate to the change and
record meaningful live acceptance for runtime/snapshot changes. Git-only or
documentation-only changes need staged-diff and repository-status checks.
