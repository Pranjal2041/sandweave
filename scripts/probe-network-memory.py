#!/usr/bin/env python3
"""Profile a bounded network workload in a disposable local SDK sandbox."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import threading
import time

from sandweave import CPU, Memory, Sandbox
from sandweave.sandbox import workspace
from sandweave.sandbox.targets import local_connection
from sandweave.sandbox.errors import SandboxError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--template', default='docker')
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--command', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    fs = shutil.disk_usage(workspace.local_parent().parent)
    if fs.free - 12 * 1024**3 < fs.total * .15:
        raise RuntimeError('Insufficient scratch headroom for this probe')
    try:
        with Sandbox(template=args.template, profiling=True,
                     cpu=CPU(vcpus=4), memory=Memory('2GiB', '2GiB')) as env:
            root = workspace.prepare()
            sys.path.insert(0, str(root / 'scripts'))
            from environment import EnvironmentManager
            manager = EnvironmentManager(root)
            state = manager.status(env.id)
            pid = state['sentry']['pid']
            (args.output / 'sandbox.json').write_text(json.dumps(
                {'id': env.id, 'pid': pid, 'worker': str(root)}, indent=2))
            stop = threading.Event()
            started = time.monotonic()
            def observe():
                with (args.output / 'memory.jsonl').open('w') as stream:
                    while not stop.wait(1):
                        try:
                            fields = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
                            sample = {'seconds': time.monotonic() - started,
                                      **{k: fields[k].strip() for k in ('VmRSS', 'RssAnon', 'RssFile', 'Threads') if k in fields}}
                            stream.write(json.dumps(sample) + '\n'); stream.flush()
                        except FileNotFoundError:
                            return
            thread = threading.Thread(target=observe, daemon=True)
            thread.start()
            def profile(label):
                env.profile(args.output / (label + '.pprof'))
            try:
                profile('before')
                process = env.exec(args.command)
                deadline = time.monotonic() + args.seconds
                next_profile = time.monotonic() + 20
                while time.monotonic() < deadline:
                    fs = shutil.disk_usage(root)
                    if fs.free - 12 * 1024**3 < fs.total * .15:
                        process.terminate()
                        raise RuntimeError('Scratch headroom fell below the probe safety floor')
                    if time.monotonic() >= next_profile:
                        profile('heap-' + str(round(time.monotonic() - started)))
                        next_profile = time.monotonic() + 20
                    result = process.poll()
                    if result is not None:
                        break
                    time.sleep(1)
                else:
                    process.terminate()
                process.wait(timeout=20)
                result = process.result()
                (args.output / 'command.stdout').write_text(result.stdout)
                (args.output / 'command.stderr').write_text(result.stderr)
                (args.output / 'result.json').write_text(json.dumps({'returncode': result.returncode, 'seconds': time.monotonic()-started}))
                profile('after')
            finally:
                stop.set(); thread.join()
                logs = root / 'runs/gvisor' / env.id
                for name in ('boot.log', 'passt.log', 'relay.log'):
                    if (logs / name).is_file():
                        shutil.copy2(logs / name, args.output / name)
    finally:
        c = local_connection()
        try:
            try:
                c.call('_shutdown_if_idle')
            except SandboxError as error:
                if str(error) != 'worker still owns live sandboxes':
                    raise
        finally:
            c.close()


if __name__ == '__main__':
    main()
