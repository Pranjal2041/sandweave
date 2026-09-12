# Standalone no-KVM lab

The ultimate goal is fast, capable sandboxes without host sudo. This extends
beyond the currently released Gym Anything environments to VR, robotics,
physical embodiment and other workloads. Do not redefine the goal around an
individual application or a current implementation choice. gVisor is an
isolation engine; Xvnc/Wayland are display infrastructure and Monado is an XR
runtime. Their roles are distinct, and multiple interaction backends belong
in the scope. The project aims to serve sandboxes with low latency at scale.
Test coordination and verification changes under concurrent load, including
their effect on unrelated work. VR gameplay and presentation-path comparisons
remain part of runtime coverage.

Work here is independent of Gym Anything's main repository. Runtime experiments
use the existing Slurm allocation without host sudo or KVM. Preserve user-facing
desktops when testing; use separate disposable environments for new tests.

## Communication and scope

- Answer specific questions with a single word or short phrase when sufficient.
  Explain further only when asked or necessary to answer correctly. Do not append
  unsolicited explanations, recaps, or unrelated details.
- The README's public examples define the agreed v1 contract for the SDK/CLI
  and the future public repository. Preserve its examples and semantics,
  including single command strings for `run`/`exec`. Keep the detailed API notes
  consistent; public contract changes require an explicit agreed revision.
- Do not give recommendations unless the user explicitly asks for them. Answer
  information requests with findings, evidence, limitations and open questions.
- When the user asks to investigate, carry out the authorized investigation;
  do not substitute a recommendation to investigate later.
- Display backends are complementary options. Preserve Xvnc support while
  investigating Wayland; Monado/XR and controller support are additional
  capabilities, not reasons to select a single exclusive desktop backend.
- Use Xvnc for future testing by default. Keep the experimental Wayland path
  available as an explicit option; switch when the user requests Wayland testing.
- Respect explicit research-only or no-execution instructions. Source and
  documentation findings must not be presented as successful runtime tests.
- Harbor integration is deferred; preserve the decisions in
  [notes/harbor-integration.md](notes/harbor-integration.md). Use direct Sandweave
  sandboxes, with benchmark-specific settings and any necessary supporting code
  kept with the benchmark template. Do not restart this work during the user's
  detour or substitute nested Docker for the planned integration.

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
- The user has given standing authorization to sync completed changes to GitHub
  and PyPI. Include the required version, validation and release steps; do not
  stop at local commits. Publish documentation updates when affected, and verify
  that the remote source and published artifacts match the completed changes.

## Validation

- Every VR demo must include recorded videos of both the left and right eye.
  Export separate eye videos and a synchronized side-by-side preview from the
  same stereo capture. Inspect decoded frames from the actual videos before
  declaring the demo verified; screenshots alone do not satisfy this requirement.
  Report capture cadence separately from application FPS and retain timing data.

Read `README.md`, `notes/gvisor-lab-reproduction.md` and the relevant implementation
notes before changing runtime behavior. Run checks appropriate to the change and
record meaningful live acceptance for runtime/snapshot changes. Git-only or
documentation-only changes need staged-diff and repository-status checks.
