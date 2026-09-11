# Proxy policy acceptance

Validated on 2026-09-11 for Sandweave 0.2.10, using disposable coding sandboxes
and two independent workers in the existing Babel allocation. No host sudo,
KVM or engine rebuild was needed. Private proxy credentials are outside Git.

The public contract is documented in [proxy policies](../docs/networking.md#proxy-policies).
`ProxyPolicy(distribution="random", region=None)` is configuration. A standalone
sandbox chooses one eligible endpoint; each pool owns its distribution state.

## What was exercised

- All four distributions on standalone sandboxes with a fixed region.
- All four distributions on a cluster pool spanning two worker processes.
  Shared-proxy members used one exit. Round-robin and shared-region members
  distributed four assignments evenly across the selected region's two proxies.
- Local round-robin pools, both constructor configuration and a user TOML
  template. Three successive members used the first, second, then first proxy;
  preparing the baseline did not consume a rotation position.
- Local named-pool creation and execution through separate CLI processes,
  including `--proxy-policy` and `--proxy-region`.
- Controller termination with SIGKILL, restart, and `Pool.connect()`. The next
  member used the second proxy after the first had been used before the crash.
  Only the test controller was signalled, after verifying its process arguments.
- Live snapshot restoration retained the selected proxy and region. A
  filesystem-cache restore selected a proxy in another region. Explicit pool
  policies rejected a memory baseline before launching pool members.
- Existing proxy checks for concurrent distinct exits, blocked direct egress,
  proxied setup, live restoration, and filesystem-cache/offline overrides.

Host tests also exercised 64 concurrent assignment transactions, rollback after
an injected write-path failure, persisted cursor recovery, lost worker replies,
unchanged claim assignments, region catalog ordering after wire serialization,
credential redaction, and rejection of workers that lack policy support.

The source host suite passed 359 tests, with four optional tests skipped. The
live cases are in `tests/integration/test_proxy_policy_live.py` and
`tests/integration/test_proxy_network.py`. Local logs are under
`runs/proxy-policy/`; release validation and artifact checksums are under
`runs/deploy/0.2.10-<commit>/`.

## Reproduce

Use a private credential file and an isolated integration directory. The live
fixture requires at least four eligible CPUs and starts two workers with
separate CPU sets. Its credential fixture accepts the private test account's
row format (`username`, `password`, `host`, `port`); the SDK accepts URL strings
or a region-to-URLs mapping. Update the test's `regions` fixture to match the
test account's country labels and supply two proxies in each tested region.

```bash
SANDWEAVE_HOME=/path/to/isolated-client \
SANDWEAVE_ASSETS=/path/to/prepared-runtime \
SANDWEAVE_TEST_PROXIES=/private/test-proxies.json \
SANDWEAVE_WEAVE_INTEGRATION=/path/to/isolated-workers \
python -m pytest tests/integration/test_proxy_policy_live.py tests/integration/test_proxy_network.py
```

This verifies assignment and lifecycle behavior, not provider capacity under
large request volumes. The separate [QUEST-RL assessment](quest-proxy-assessment.md)
records search and source-page access through the supplied proxies.
