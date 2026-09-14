#!/usr/bin/env python3
"""Exercise task setup, screenshots and canonical evaluation through Benchmark.

This is an infrastructure smoke test. Its scores describe untouched tasks;
they are not agent results. Keep the output directory outside the source tree
or under an ignored runs directory.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import time
import traceback

from sandweave import Benchmark


def audit(bench, output, task_ids=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    selected = set(task_ids or ())
    with (output / 'results.jsonl').open('a', buffering=1) as report:
        for task in bench:
            if selected and task.id not in selected:
                continue
            directory = output / task.id
            directory.mkdir(exist_ok=True)
            result = {'task': task.id, 'kind': 'untouched-task-smoke-test'}
            started = time.monotonic()
            print('START', task.id, flush=True)
            try:
                with task as env:
                    result.update(sandbox=env.id, ready_seconds=time.monotonic() - started)
                    screenshot = env.desktop.screenshot()
                    assert screenshot.size == (1920, 1080), screenshot.size
                    screenshot.save(directory / 'initial.png')
                    result['evaluation'] = asdict(task.evaluate())
                    result['status'] = 'completed'
            except Exception:
                result.update(status='error', error=traceback.format_exc())
            result['seconds'] = time.monotonic() - started
            report.write(json.dumps(result) + '\n')
            print(json.dumps(result), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--task', action='append')
    args = parser.parse_args()
    with Benchmark('osworld-energy50-representative', source=args.source, capacity=1) as bench:
        audit(bench, args.output, args.task)


if __name__ == '__main__':
    main()
