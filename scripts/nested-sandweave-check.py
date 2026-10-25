#!/usr/bin/env python3
"""Exercise the installed Sandweave SDK without kernel or installer overrides."""
import argparse
import json
import time

from sandweave import Sandbox


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', default='coding')
    parser.add_argument('--gpu', action='store_true')
    args = parser.parse_args()
    started = time.monotonic()
    options = {'gpu': True} if args.gpu else {}
    with Sandbox(template=args.template, **options) as sandbox:
        print(json.dumps({'event': 'ready', 'seconds': time.monotonic() - started}), flush=True)
        commands = ['uname -a', 'id', 'python3 -c "print(sum(range(10000)))"']
        if args.gpu:
            commands.append('nvidia-smi -L')
        for command in commands:
            result = sandbox.run(command)
            print(json.dumps({'command': command, 'returncode': result.returncode,
                              'stdout': result.stdout, 'stderr': result.stderr}), flush=True)
            if result.returncode:
                raise RuntimeError('Guest command failed: ' + command)
    print(json.dumps({'event': 'closed', 'seconds': time.monotonic() - started}), flush=True)


if __name__ == '__main__':
    main()
