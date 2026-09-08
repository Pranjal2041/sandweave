#!/usr/bin/env python3
import argparse
from pathlib import Path
import shutil
import json
import runtime_store
import subprocess

parser = argparse.ArgumentParser(description='Build and publish an immutable gVisor runtime.')
parser.add_argument('--publish-only', action='store_true',
                    help='publish a candidate without changing the default runtime for new launches')
args = parser.parse_args()
lab = Path(__file__).resolve().parent.parent
# runsc and Sentry are separate Bazel targets. Build the complete publication
# here so an incremental runsc-only build cannot silently stage an old Sentry.
subprocess.run([str(lab / 'scripts/build-gvisor.sh'), 'build', '//runsc:runsc',
                '//runsc/cmd/sentry:gvisor_sentry',
                '//runsc/checkpointgofer:checkpointgofer_binary',
                '//runsc/prewarmer:gvisor-sentry-prewarmer',
                '//runsc/cmd/metricserver:runsc-metric-server'], check=True)
local = (lab / 'runs/local-path.txt').read_text().strip()
base = Path(str((lab / 'sources/gvisor/bazel-bin').readlink()).replace('/local', local, 1))
stage = lab / 'tools/gvisor-socket'
(stage / 'gvisor-bin').mkdir(parents=True, exist_ok=True)
artifacts = [
    ('runsc/runsc_/runsc', 'runsc'),
    ('runsc/cmd/sentry/gvisor_sentry_/gvisor_sentry', 'gvisor-bin/gvisor_sentry'),
    ('runsc/checkpointgofer/checkpointgofer_binary_/checkpointgofer_binary', 'gvisor-bin/checkpointgofer'),
    ('runsc/prewarmer/gvisor-sentry-prewarmer', 'gvisor-bin/gvisor-sentry-prewarmer'),
    ('runsc/cmd/metricserver/runsc-metric-server_/runsc-metric-server', 'gvisor-bin/runsc-metric-server'),
]
descriptor = runtime_store.publish(lab, {destination: base / source for source, destination in artifacts})
for source, destination in artifacts:
    if args.publish_only:
        continue
    # Atomic replacement permits an existing guest to finish on its old binary.
    dest = stage / destination
    temp = dest.with_name(dest.name + '.new')
    shutil.copy2(base / source, temp)
    temp.replace(dest)
    print(destination)
if not args.publish_only:
    (stage / 'runtime.json').write_text(json.dumps(descriptor, indent=2) + '\n')
print(descriptor['path'])
