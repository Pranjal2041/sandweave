# Troubleshooting

## Setup is taking time

First use downloads runtime files and installs software. Setup shows the current
stage, elapsed time, and recent build output. Full logs are in `logs/setup` under
your selected Sandweave storage directory.

A later sandbox reuses prepared files. If it builds the engine from source,
the compatibility check did not select a matching prebuilt runtime, or you
explicitly requested `--build`.

Run `sandweave doctor` to inspect and repair the installation. A host policy that
blocks unprivileged container launches must be addressed on that host; installing
the Python package cannot grant host permissions.

## Where did it store files?

Each project defaults to its own `.sandweave` directory. Setup prints the chosen
destination. If you selected another location, the project's
`.sandweave/location.json` points there. An explicit `SANDWEAVE_HOME` overrides
that selection.

Another project does not automatically reuse this installation. See
[installation](installation.md#choose-where-files-go) for explicit sharing.

## `lab` is not configured on another machine

`lab` is a saved project-local connection name. Paste the full HTTP or SSH address
printed by the controller as `target`, including the HTTP token fragment.
Run `sandweave cluster instructions lab` in the controller project to print it
again. See [connect a cluster](clusters.md).

## The controller prints a local HTTP address

A URL labelled “this machine only” comes from a loopback listener. New controllers
accept direct HTTP by default starting with 0.2.3. Upgrade and restart an older
controller to load new defaults. Explicit listener settings remain in effect;
an intentionally local listener remains local.

A printed hostname still needs a network route from your client. HTTP address
printing cannot create DNS records, firewall rules, or cross-network routing.
The printed SSH command uses your existing SSH access.

## Sandbox creation is waiting

Check worker registration and available capacity in the dashboard or with
`sandweave cluster workers lab`. A controller started with `--no-worker` has no
worker capacity until a machine joins. A GPU or memory request must fit an
eligible worker's available resources.

## `env.close()` left it running

`close()` disconnects the handle. It does not terminate the sandbox. Use
`env.terminate()` to release the runtime, or `env.stop()` to save before release.
A terminated sandbox can remain in dashboard history.

An interrupted notebook cell leaves its kernel alive. Default automatic cleanup
follows the Python process, not the current cell. See [cleanup](lifecycle.md).

## A program printed an error

Read `result.stderr` and `result.returncode`. Nonzero exits return results by
default. `check=True` raises `CommandError` for those exits. Infrastructure and
connection failures remain exceptions; they are different from a guest program
failing normally.

## Packages cannot install in an offline sandbox

`network="offline"` blocks guest downloads. Prepare packages with internet access
and save a cache, then restore it offline. Initial worker preparation may still
download runtime or template files. See [networking](networking.md).

## VNC is not reachable from my browser's machine

`env.info["vnc"]` reports the worker's loopback URL, port, and password. Arrange
remote VNC access through your own network or tunnel. The cluster dashboard
and sandbox command transport do not forward that separate VNC connection.

## A dashboard sign-in link expired

`sandweave dashboard lab` creates a single-use link valid for 60 seconds. Run it
again for a fresh link. The startup dashboard link printed by
`sandweave cluster instructions lab` is reusable while the controller address
and credential stay unchanged.

## Report a reproducible issue

Open a [GitHub issue](https://github.com/Pranjal2041/sandweave/issues) with your
Sandweave version, the command or short Python example, the failing stage, and
relevant log output. Remove tokens, passwords, and private connection links
before sharing logs.
