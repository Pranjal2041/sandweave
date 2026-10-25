#!/usr/bin/env python3
"""Collect guest startup diagnostics while a named SDK creation is in progress."""
import argparse
import json
from pathlib import Path
import time
import uuid

from sandweave.sandbox.runtimes.gvisor.driver import Runtime
from sandweave.sandbox.wire import decode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', required=True, type=Path)
    parser.add_argument('--name', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    deadline = time.monotonic() + 300
    record = None
    while time.monotonic() < deadline:
        for path in args.records.glob('*.bin'):
            value = decode(path.read_bytes())
            if value.get('name') == args.name and value.get('agent'):
                record = value
                break
        if record:
            break
        time.sleep(.2)
    if record is None:
        raise TimeoutError('named sandbox did not start its agent')
    print(record['id'], flush=True)
    commands = {
        'journal': 'journalctl --boot --quiet --no-pager -n 400 -o json',
        'startup-message': 'journalctl --boot --quiet --no-pager -o json '
                           'MESSAGE_ID=f3ea493c22934e26811cd62abe8e203a',
        'status': 'systemctl --failed --no-pager; ps -eo pid,uid,pcpu,comm; '
                  'ls -l /run/log/journal; cat /proc/sys/kernel/random/boot_id',
    }
    # Startup holds the worker's lifecycle lock. Use its existing guest agent
    # for read-only diagnostics so collection can proceed during that startup.
    runtime = Runtime(args.records.parent)
    agent = runtime.agent(record['id'], record['agent'])
    try:
        for index in range(8):
            for name, command in commands.items():
                try:
                    process = uuid.uuid4().hex
                    agent.call('spawn', identity=process, argv=['/bin/sh', '-c', command],
                               user='root', cwd='/', env={}, timeout=15, max_output_bytes=1024**2)
                    agent.call('stdin', identity=process, close=True)
                    while (state := agent.call('wait', identity=process, timeout=10))['returncode'] is None:
                        pass
                    data = {'returncode': state['returncode'], **{
                        stream: agent.call('output', identity=process, stream=stream,
                                           size=1024**2).decode(errors='replace')
                        for stream in ('stdout', 'stderr')}}
                except Exception as error:
                    data = {'type': type(error).__name__, 'error': str(error)}
                (args.output / f'{index}-{name}.json').write_text(json.dumps(data, indent=2) + '\n')
            time.sleep(10)
    finally:
        runtime.detach(record['id'])


if __name__ == '__main__':
    main()
