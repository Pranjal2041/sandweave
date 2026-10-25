#!/usr/bin/env python3
"""Run a probe binary or script in an existing sandbox, concurrently, and retain each result."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from sandweave import Sandbox


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sandbox')
    parser.add_argument('binary', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--parallel', type=int, default=1)
    parser.add_argument('--timeout', type=float, default=600)
    parser.add_argument('--interpreter', help='guest interpreter for a script probe, e.g. python3')
    parser.add_argument('--user')
    parser.add_argument('argv', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    args.output.mkdir(parents=True, exist_ok=False)
    remote = '/tmp/' + args.binary.name
    with Sandbox.connect(args.sandbox) as sandbox:
        sandbox.files.write_bytes(remote, args.binary.read_bytes(), mode=0o755)

        command = [args.interpreter, remote] if args.interpreter else [remote]

        def probe(index):
            result = sandbox.run(argv=[*command, *argv], timeout=args.timeout, user=args.user)
            return {'index': index, 'returncode': result.returncode,
                    'stdout': result.stdout, 'stderr': result.stderr}
        with ThreadPoolExecutor(args.parallel) as pool:
            results = list(pool.map(probe, range(args.parallel)))
    failed = [result['index'] for result in results if result['returncode']]
    report = {'sandbox': args.sandbox, 'argv': argv, 'parallel': args.parallel,
              'failed': failed, 'results': results}
    (args.output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'parallel': args.parallel, 'failed': failed}), flush=True)
    for result in results:
        if result['returncode']:
            print(json.dumps(result), flush=True)
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    main()
