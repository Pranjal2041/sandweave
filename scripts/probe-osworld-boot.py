#!/usr/bin/env python3
"""Boot an owned filesystem snapshot with a candidate engine, without KVM."""
import argparse
from pathlib import Path

from environment import EnvironmentManager
import runtime_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--build-directory', type=Path, required=True)
    parser.add_argument('--name', required=True)
    args = parser.parse_args()
    files = {
        'runsc': 'runsc/runsc_/runsc',
        'gvisor-bin/gvisor_sentry': 'runsc/cmd/sentry/gvisor_sentry_/gvisor_sentry',
        'gvisor-bin/checkpointgofer': 'runsc/checkpointgofer/checkpointgofer_binary_/checkpointgofer_binary',
        'gvisor-bin/gvisor-sentry-prewarmer': 'runsc/prewarmer/gvisor-sentry-prewarmer',
        'gvisor-bin/runsc-metric-server': 'runsc/cmd/metricserver/runsc-metric-server_/runsc-metric-server',
    }
    runtime = runtime_store.publish(args.worker, {name: args.build_directory / source
                                                  for name, source in files.items()})
    manager = EnvironmentManager(args.worker)
    manager.load(args.snapshot, args.name, command=['/bin/bash', '-c',
        'set -e; for i in $(seq 0 63); do mknod -m 600 /dev/tty$i c 4 $i; done; '
        'exec /sbin/init'], options=['--runtime-build', runtime['path'], '--virtual-consoles', '--no-runtime-debug'], timeout=300)
    print('Started', args.name, 'with', runtime['path'], flush=True)
    print('Logs:', manager._logs(args.name), flush=True)


if __name__ == '__main__':
    main()
