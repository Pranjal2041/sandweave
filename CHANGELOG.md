# Changelog

## 0.2.2

- Setup streams guest images and desktop/VR helpers to the host installer,
  which writes them in the selected storage directory as the host user.
  Guest ownership no longer needs to map to the destination's host group.
- Build output is accepted only after both archives pass checksum verification
  and the guest exits successfully. Interrupted transfers publish no output.
- Release validation exercises the installed transfer path with private,
  inherited-group and default-ACL directories where available.

## 0.2.1

- Storage settings are local to each project. Setup no longer reads the old
  home-directory location setting, discovers runtimes in parent directories,
  or imports another installation's runtime and cluster settings when choosing
  a new directory. Existing installations remain available through an explicit
  `SANDWEAVE_HOME` or setup selection.
- Cluster startup prints complete join and dashboard commands. `--transport`
  selects SSH, HTTP or HTTPS; HTTP(S) join links include authentication.
  `cluster instructions` prints the commands again. Worker join checks the
  connection before preparing runtime files. `cluster start --json` retains
  machine-readable status output.

## 0.2.0

- `sandweave dashboard` opens a read-only cluster dashboard with live resource
  measurements, retained charts, worker/GPU/workload views, events, and bounded
  logs. It uses the controller's HTTP, HTTPS, or SSH connection. Authenticated
  Prometheus metrics are available at `/metrics`.

- Weave adds named clusters over existing local, SSH and Slurm workers.
  `Sandbox(target="lab")` and `Pool(target="lab")` use durable placement,
  weighted scheduling, ready reserves and worker draining.
- `Job.submit(...)` stores commands, inputs, attempts and results, with explicit
  retries, cancellation, batches and repeat intervals. Controllers can restart
  without restarting active commands.
- Workers enforce assignment generations and sandbox-specific credentials.
  Verified snapshots can transfer between worker storage directories.
- First-use cluster setup retains the selected storage directory and prepares
  missing templates on workers. Idle control connections reconnect before a new
  request; mutations with uncertain delivery still report that uncertainty.

## 0.1.2

- `env.run(...)` and `await env.run.aio(...)` now default to `check=False`.
  Failed commands return their stdout, stderr and exit code. Pass `check=True`
  to keep raising `CommandError`. Timeouts and connection failures still raise.
- `sandweave.__version__` reads the installed package version.
- Maintainers can validate and publish an SDK release with `./deploy`.

## 0.1.1

- Setup downloads compatible, verified runtime binaries from GitHub Releases
  and falls back to building from source when needed.
