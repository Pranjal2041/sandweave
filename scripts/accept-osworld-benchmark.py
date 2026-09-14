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
import random
import secrets
import time
import traceback

from sandweave import Benchmark
from sandweave.benchmarks.osworld import OSWorld


class LoggedSetup:
    """Record public setup commands without changing the integration's behavior."""
    def __init__(self, env, directory):
        self.env, self.directory = env, directory
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self.env, name)

    def run(self, *args, **kwargs):
        started = time.monotonic()
        result = None
        self.calls += 1
        name = f'setup-{self.calls:02d}'
        record = {'args': args, 'argv': kwargs.get('argv'),
                  'user': kwargs.get('user'), 'status': 'error'}
        try:
            result = self.env.run(*args, **kwargs)
            record['status'] = 'completed'
            return result
        except Exception as exc:
            result = getattr(exc, 'result', None)
            record['error'] = str(exc)
            raise
        finally:
            record['seconds'] = time.monotonic() - started
            if result is not None:
                record['returncode'] = result.returncode
                for stream in ('stdout', 'stderr'):
                    value = getattr(result, stream)
                    path = self.directory / f'{name}.{stream}.log'
                    if isinstance(value, bytes):
                        path.write_bytes(value)
                    else:
                        path.write_text(value)
            (self.directory / f'{name}.json').write_text(json.dumps(record, indent=2) + '\n')


class AuditedOSWorld(OSWorld):
    def __init__(self, source, output):
        super().__init__('osworld-energy50-representative', source=source)
        self.output = output

    def setup(self, env, task):
        directory = self.output / task.id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'task.json').write_text(json.dumps(asdict(task), indent=2) + '\n')
        started = time.monotonic()
        try:
            super().setup(LoggedSetup(env, directory), task)
        except Exception:
            try:
                env.desktop.screenshot().save(directory / 'setup-failure.png')
            except Exception:
                (directory / 'screenshot-error.txt').write_text(traceback.format_exc())
            raise
        finally:
            (directory / 'setup-seconds.json').write_text(json.dumps(time.monotonic() - started))
            # Retain diagnostics before the task context releases its sandbox.
            # Diagnostic failures must not replace the original setup exception.
            for path in ('/tmp/osworld-launch.log', '/var/log/osworld-build.log',
                         '/var/log/sandweave-osworld-readiness.json'):
                try:
                    (directory / Path(path).name).write_text(env.files.read_text(path))
                except Exception as exc:
                    (directory / (Path(path).name + '.error')).write_text(str(exc))


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
    parser.add_argument('--sample', type=int, help='randomly select this many tasks without replacement')
    parser.add_argument('--seed', type=int, help='reproduce a random selection; otherwise generate and record a seed')
    parser.add_argument('--cache', help='reuse a previously prepared benchmark baseline')
    parser.add_argument('--actions', type=Path, help='JSON mapping task ids to recorded GUI actions and expected_score')
    parser.add_argument('--prepare-only', action='store_true', help='prepare the client evaluator without starting sandboxes')
    args = parser.parse_args()
    if args.prepare_only:
        if args.cache or args.actions or args.task or args.sample is not None or args.seed is not None:
            parser.error('--prepare-only cannot be combined with task or sandbox options')
        prepare_evaluator(args.source, args.output)
        return
    options = {'template': None, 'cache': args.cache} if args.cache else {}
    if args.sample is not None and (args.task or args.actions):
        parser.error('--sample cannot be combined with --task or --actions')
    if args.seed is not None and args.sample is None:
        parser.error('--seed requires --sample')
    suite = AuditedOSWorld(args.source, args.output)
    if args.sample is not None:
        if not 1 <= args.sample <= len(suite.tasks):
            parser.error('--sample must be between 1 and the number of tasks')
        seed = args.seed if args.seed is not None else secrets.randbits(64)
        args.task = random.Random(seed).sample([task.id for task in suite.tasks], args.sample)
        selection = {'seed': seed, 'population': len(suite.tasks), 'tasks': args.task}
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'selection.json').write_text(json.dumps(selection, indent=2) + '\n')
        print(json.dumps(selection), flush=True)
    with Benchmark(suite, capacity=1, **options) as bench:
        errors = audit(bench, args.output, args.task,
                       json.loads(args.actions.read_text()) if args.actions else None)
    raise SystemExit(bool(errors))


if __name__ == '__main__':
    main()
