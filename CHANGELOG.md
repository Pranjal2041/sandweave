# Changelog

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
