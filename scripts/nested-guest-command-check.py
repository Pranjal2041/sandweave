#!/usr/bin/env python3
"""Exercise concurrent short guest commands in an existing diagnostic sandbox."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

from sandweave import Sandbox


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sandbox')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with Sandbox.connect(args.sandbox) as sandbox:
        def command(index):
            try:
                expected = f'command-complete-{index}'
                result = sandbox.run('printf ' + expected, timeout=15)
                if result.returncode or result.stdout != expected:
                    raise RuntimeError('unexpected command result')
                return None
            except Exception as error:
                return {'index': index, 'type': type(error).__name__, 'error': str(error)}
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(command, range(1000)))
        failures = [result for result in results if result is not None]
        report = {'commands': len(results), 'failures': failures}
        (args.output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
        if failures:
            result = sandbox.run('journalctl --boot --quiet --no-pager --output=cat -n 160 '
                                 '_SYSTEMD_UNIT=sandweave-agent.service', user='root', timeout=15)
            (args.output / 'agent-errors.txt').write_text(result.stdout + result.stderr)
        print(json.dumps({'commands': len(results), 'failed': len(failures)}), flush=True)
        if failures:
            raise RuntimeError('Concurrent guest commands failed; diagnostics were preserved')


if __name__ == '__main__':
    main()
