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


def prepare_evaluator(source, output):
    """Exercise first-use evaluator installation without starting a desktop."""
    from sandweave.benchmarks.osworld import OSWorld

    started = time.monotonic()
    suite = OSWorld('osworld-energy50-representative', source=source)
    try:
        suite.prepare()
        process = suite.evaluators._start()
        suite.evaluators._stop(process)
        result = {'tasks': len(suite.tasks), 'seconds': time.monotonic() - started,
                  'python': suite.evaluators.python, 'status': 'ready'}
        output.mkdir(parents=True, exist_ok=True)
        (output / 'evaluator.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)
    finally:
        suite.close()


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
                        replay = actions[task.id]
                        result['kind'] = ('terminal-action' if all(isinstance(action, str)
                                          for action in replay['actions']) else 'recorded-gui-actions')
                        result['before'] = asdict(task.evaluate())
                        if 'initial_score' in replay:
                            assert result['before']['score'] == replay['initial_score'], result
                        result['action_seconds'] = []
                        for index, action in enumerate(replay['actions']):
                            action_started = time.monotonic()
                            env.desktop.action(action)
                            env.desktop.screenshot().save(directory / f'action-{index:03d}.png')
                            result['action_seconds'].append(time.monotonic() - action_started)
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
    parser.add_argument('--prepare-only', action='store_true', help='prepare the client evaluator without starting sandboxes')
    args = parser.parse_args()
    if args.prepare_only:
        if args.cache or args.actions or args.task:
            parser.error('--prepare-only cannot be combined with --cache, --actions or --task')
        prepare_evaluator(args.source, args.output)
        return
    options = {'template': None, 'cache': args.cache} if args.cache else {}
    with Benchmark('osworld-energy50-representative', source=args.source, capacity=1, **options) as bench:
        errors = audit(bench, args.output, args.task,
                       json.loads(args.actions.read_text()) if args.actions else None)
    raise SystemExit(bool(errors))


if __name__ == '__main__':
    main()
