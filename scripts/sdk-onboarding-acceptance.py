#!/usr/bin/env python3
"""Live CPU acceptance for switching setup inputs without disrupting existing guests."""
import argparse
import json
import os
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--assets', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--slurm-job', help='Also test a borrowed Slurm worker using the same allocation')
args = parser.parse_args()
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
metadata = output / 'result.json'
if metadata.exists():
    raise SystemExit('Use a fresh output directory')
os.environ['SANDWEAVE_HOME'] = str(output / 'home')
os.environ.pop('SANDWEAVE_ASSETS', None)

from sandweave import Sandbox, Slurm
from sandweave.onboarding import save_configuration
from sandweave.sandbox import workspace
from sandweave.sandbox.targets import local_connection

save_configuration(assets=str(args.assets.resolve()))
prepared = workspace.prepare()
alternate = output / 'alternate'
for name in ('tools', 'images', 'sandweave-assets.json'):
    workspace.stage_tree(prepared / name, alternate / name)

# Workers must find setup-installed executables even with no Apptainer on PATH.
# The wrapper records actual launches before delegating to the existing binary.
apptainer = workspace.tool('apptainer')
assert apptainer
managed = workspace.home() / 'bin'
managed.mkdir()
wrapper = managed / 'apptainer'
marker = output / 'managed-apptainer.log'
import shlex
wrapper.write_text('#!/bin/sh\n' + 'printf "launch\\n" >> ' + shlex.quote(str(marker)) + '\n' +
                   'exec ' + shlex.quote(apptainer) + ' "$@"\n')
wrapper.chmod(0o755)
connections = []
result = {}
try:
    with Sandbox() as first:
        connections.append(first._connection)
        first.files.write_text('/workspace/keep.txt', 'original guest')
        save_configuration(assets=str(alternate))
        with Sandbox() as second:
            connections.append(second._connection)
            assert first._connection.port != second._connection.port
            assert first.files.read_text('/workspace/keep.txt') == 'original guest'
            assert second.run('test ! -e /workspace/keep.txt', check=False).returncode == 0
            assert second.run("python -c 'print(2 + 2)'").stdout.strip() == '4'
            result['asset_switch'] = {'distinct_workers': True, 'original_guest_preserved': True,
                                      'new_guest_independent': True}
    if args.slurm_job:
        allocation = Slurm.connect(args.slurm_job)
        with Sandbox(target=allocation) as env:
            connections.append(env._connection)
            assert env.run("python -c 'print(2 + 2)'").stdout.strip() == '4'
        result['borrowed_slurm'] = {'job': args.slurm_job, 'sandbox_passed': True}
    assert marker.is_file() and marker.read_text().count('launch') >= 2
    result['managed_apptainer'] = True
finally:
    # Context exit closes client connections, so reconnect only to these owned workers.
    from sandweave.sandbox.connection import Connection
    for previous in connections:
        connection = Connection(previous.host, previous.port, previous.token)
        try:
            connection.call('_shutdown_if_idle')
        finally:
            connection.close()
metadata.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
