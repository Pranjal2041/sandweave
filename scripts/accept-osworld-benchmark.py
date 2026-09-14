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


def audit(bench, output, task_ids=None, actions=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    selected = set(task_ids or ())
    actions = actions or {}
    unknown = (selected | actions.keys()) - {task.id for task in bench.tasks}
    if unknown:
        raise ValueError('unknown task ids: ' + ', '.join(sorted(unknown)))
    errors = 0
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
                    result['application_readiness'] = json.loads(env.files.read_text(
                        '/var/log/sandweave-osworld-readiness.json'))
                    if task.id in actions:
                        result['kind'] = 'recorded-gui-actions'
                        result['before'] = asdict(task.evaluate())
                        for action in actions[task.id]['actions']:
                            env.desktop.action(action)
                        env.desktop.screenshot().save(directory / 'after.png')
                    result['evaluation'] = asdict(task.evaluate())
                    if task.id in actions:
                        assert result['evaluation']['score'] == actions[task.id]['expected_score'], result
                    result['status'] = 'completed'
            except Exception:
                errors += 1
                result.update(status='error', error=traceback.format_exc())
            result['seconds'] = time.monotonic() - started
            report.write(json.dumps(result) + '\n')
            print(json.dumps(result), flush=True)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--task', action='append')
    parser.add_argument('--cache', help='reuse a previously prepared benchmark baseline')
    parser.add_argument('--actions', type=Path, help='JSON mapping task ids to recorded GUI actions and expected_score')
    args = parser.parse_args()
    options = {'template': None, 'cache': args.cache} if args.cache else {}
    with Benchmark('osworld-energy50-representative', source=args.source, capacity=1, **options) as bench:
        errors = audit(bench, args.output, args.task,
                       json.loads(args.actions.read_text()) if args.actions else None)
    raise SystemExit(bool(errors))


if __name__ == '__main__':
    main()
