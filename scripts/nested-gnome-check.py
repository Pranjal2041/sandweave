#!/usr/bin/env python3
"""Check repeated complete desktop startups and retain a failed guest for diagnosis."""
import argparse
import json
from pathlib import Path
import time
import traceback

from sandweave import Sandbox


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--iterations', type=int, default=5)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    for index in range(args.iterations):
        started = time.monotonic()
        try:
            with Sandbox(template='gnome', detached=True, keep_on_error=True) as sandbox:
                sandbox.desktop.screenshot().save(args.output / f'{index}-desktop.png')
                result = sandbox.run('printf desktop-command-complete', check=True)
                if result.stdout != 'desktop-command-complete':
                    raise RuntimeError('unexpected desktop command result')
                report = {'id': sandbox.id, 'seconds': time.monotonic() - started,
                          'timings': sandbox.timings}
                (args.output / f'{index}-ready.json').write_text(json.dumps(report, indent=2) + '\n')
                print(json.dumps(report), flush=True)
        except Exception as error:
            failure = {'type': type(error).__name__, 'error': str(error),
                       'sandbox_id': getattr(error, 'sandbox_id', None),
                       'traceback': traceback.format_exc()}
            (args.output / f'{index}-failure.json').write_text(json.dumps(failure, indent=2) + '\n')
            raise


if __name__ == '__main__':
    main()
