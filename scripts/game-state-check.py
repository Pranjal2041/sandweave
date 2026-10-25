#!/usr/bin/env python3
"""Check the frontier games' state exporters on a real Linux guest with real telemetry.

Run on a worker host. Starts a Sandweave sandbox, then for each game copies its
recorded telemetry (from an evaluation or audit run) into the guest at the
environment's own path in growing chunks, cut mid-line, observing
sandweave.games.state.GameState through the public SDK after each chunk. Checks
that the last incremental observation equals a fresh full read, that every byte
was consumed once, and that guest time increases. Finally runs one unified step
observing 'state'.

    scripts/game-state-check.py --output DIR --speed-dreams JOURNAL --open-saber JOURNAL \
        --openttd GAME_LOG --wesnoth GAME_LOG --openra REPLAY --openra-reader openra_replay.py
"""
import argparse
import json
from pathlib import Path

from sandweave import Sandbox
from sandweave.games.state import GameState
from sandweave.steps import run as run_step

TARGETS = {'speed-dreams': '/var/lib/sdracer/eval/journal.jsonl', 'openttd': '/var/lib/ottd_task/run/game.log',
           'wesnoth': '/var/lib/wesnoth-audit/game.log', 'open-saber': '/var/lib/open-saber-session/private/journal-02.jsonl'}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    for game in ('speed-dreams', 'open-saber', 'openttd', 'wesnoth', 'openra', 'openra-reader'):
        parser.add_argument('--' + game, type=Path, required=True)
    parser.add_argument('--chunks', type=int, default=6)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {}
    with Sandbox(template='coding') as sandbox:
        for game, target in TARGETS.items():
            data = getattr(args, game.replace('-', '_')).read_bytes()
            sandbox.run(f'install -d -m 700 {Path(target).parent}', user='root', check=True)
            source = str(Path(target).parent) if game == 'open-saber' else None
            exporter, reads = GameState(game, source=source), []
            for index in range(args.chunks):
                piece = data[len(data) * index // args.chunks:len(data) * (index + 1) // args.chunks]
                with sandbox.files.open(target, 'ab') as output:
                    output.write(piece)
                reads.append(exporter.observe(sandbox))
            sandbox.run('rm -rf /run/sandweave-state/*.json', user='root', check=True)
            full = GameState(game, source=source).observe(sandbox)
            report[game] = {'bytes': len(data), 'reads': len(reads),
                            'consumed_once': sum(read['new_bytes'] for read in reads) == len(data),
                            'incremental_equals_full': reads[-1]['state'] == full['state'],
                            'guest_time_increasing': all(b['guest_ns'] > a['guest_ns'] for a, b in zip(reads, reads[1:]))}
            (args.output / f'{game}.json').write_text(json.dumps(full, indent=1) + '\n')
        sandbox.run('install -d -m 700 /var/lib/openra/support/Replays /workspace/utils', user='root', check=True)
        sandbox.files.write_bytes('/var/lib/openra/support/Replays/' + args.openra.name, args.openra.read_bytes())
        sandbox.files.write_bytes('/workspace/utils/openra_replay.py', args.openra_reader.read_bytes())
        openra = GameState('openra').observe(sandbox)
        (args.output / 'openra.json').write_text(json.dumps(openra, indent=1) + '\n')
        report['openra'] = {'game_seconds': openra['state']['game_seconds'], 'players': len(openra['state']['players']),
                            'orders': sum(openra['state']['orders'].values())}
        step = run_step(sandbox, {'id': 'state-step', 'observe': ['state']}, state=GameState('wesnoth'))
        report['step'] = {'game_time_ns': step['observations']['state']['game_time_ns'],
                          'scenario': step['observations']['state']['value']['state']['scenario']}
    report['passed'] = all(all(v for k, v in r.items() if isinstance(v, bool)) for r in report.values()
                           if isinstance(r, dict))
    (args.output / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
