# Changelog

## 0.1.2

- `env.run(...)` and `await env.run.aio(...)` now default to `check=False`.
  Failed commands return their stdout, stderr and exit code. Pass `check=True`
  to keep raising `CommandError`. Timeouts and connection failures still raise.
- `sandweave.__version__` reads the installed package version.
- Maintainers can validate and publish an SDK release with `./deploy`.

## 0.1.1

- Setup downloads compatible, verified runtime binaries from GitHub Releases
  and falls back to building from source when needed.
