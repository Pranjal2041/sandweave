#!/usr/bin/env python3
"""Verify an OSWorld audit and emit aggregate results without the private split."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from PIL import Image
from sandweave.benchmarks import source


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def records(directory):
    rows = [json.loads(line) for line in (directory / 'results.jsonl').read_text().splitlines()]
    assert len({row['task'] for row in rows}) == len(rows), 'duplicate task attempts'
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--replay', type=Path, action='append', default=[])
    parser.add_argument('--worker', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    reference = source.cua(args.source)
    selected = source.module(reference / 'scripts/build_osworld_subset.py')._read_spec(
        reference / 'benchmarks/osworld-energy50-representative/benchmark-source.yaml')['tasks']
    expected = [item['id'] for item in selected]
    original = records(args.report)
    assert [row['task'] for row in original] == expected
    provenance = json.loads((args.report / 'provenance.json').read_text())
    cases = {row['task']: (row, args.report) for row in original}
    attempts = [(row, args.report) for row in original]
    for replay in args.replay:
        for row in records(replay):
            assert row['task'] in cases
            cases[row['task']] = row, replay
            attempts.append((row, replay))
    assert len(expected) == 50
    for row, directory in attempts:
        with Image.open(directory / row['task'] / 'initial.png') as screenshot:
            screenshot.load()
            assert screenshot.size == (1920, 1080)
        run = args.worker / 'runs/gvisor' / row['sandbox']
        launch = (run / 'run.log').read_text()
        assert '/runtime-builds/' + provenance['runtime_build'] + '/runsc' in launch
        for flag in ('--application-cpus=4', '--app-memory-limit=17179869184',
                     '--runtime-memory-limit=1073741824', '--virtual-consoles=headless'):
            assert flag in launch, (row['sandbox'], flag)
        assert json.loads((run / 'stopped.json').read_text())['complete']
    fixtures = json.loads((Path(__file__).resolve().parents[1] /
                           'tests/fixtures/osworld-actions.json').read_text())
    assert fixtures.keys() <= cases.keys()
    for identity, (row, directory) in cases.items():
        assert row['status'] == 'completed', row
        assert 'application_readiness' in row and 'evaluation' in row
        if identity in fixtures:
            assert row['before']['score'] == fixtures[identity]['initial_score']
            assert row['evaluation']['score'] == fixtures[identity]['expected_score']
            assert row['evaluation']['passed']
            with Image.open(directory / identity / 'after.png') as screenshot:
                screenshot.load()
                assert screenshot.size == (1920, 1080)
    terminal = sum(all(isinstance(action, str) for action in fixture['actions'])
                   for fixture in fixtures.values())
    summary = {
        'date': datetime.now(timezone.utc).date().isoformat(), 'provenance': provenance,
        'tasks': len(cases), 'initial_attempts': len(original),
        'initial_assertion_or_runtime_failures': sum(row['status'] == 'error' for row in original),
        'replay_attempts': len(attempts) - len(original),
        'replay_assertion_or_runtime_failures': sum(row['status'] == 'error'
                                                   for row, _ in attempts[len(original):]),
        'unresolved_failures': 0,
        'domains': dict(sorted(Counter(identity.removeprefix('osworld_').rsplit('_', 1)[0]
                                       for identity in expected).items())),
        'gui_completions_zero_to_100': len(fixtures) - terminal,
        'infeasible_terminal_action_zero_to_100': terminal,
        'verified_screenshots': len(attempts) + len(fixtures),
        'runtime_and_cleanup_checks': len(attempts),
        'files': {'audit': digest(args.report / 'results.jsonl'),
                  'replays': [digest(replay / 'results.jsonl') for replay in args.replay]},
        'scope': 'Setup, screenshot and canonical evaluation audit; not an agent accuracy score.'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
