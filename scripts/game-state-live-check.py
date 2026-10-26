#!/usr/bin/env python3
"""Observe a frontier desktop game's state live while it is played (worker with gym-anything).

    game-state-live-check.py --env-dir DIR --task TASK --game GAME --exporter state_export.py --output DIR

Starts the environment and task with gym_anything as the game's own harness does
(reset(seed=42), from the post_start cache except for Wesnoth, whose harness resets
without it; the task's setup launches the game already in play). Installs sandweave's guest exporter
(src/sandweave/games/state_export.py, which GameState installs) under a root-only
directory, drives the game with its own inputs, and every --interval seconds reads
the state as root and takes a screenshot. The per-game check asserts that the
state followed the play.
"""
import argparse
import json
from pathlib import Path
import time

from gym_anything.api import from_config

GUEST = '/run/sandweave-state/state_export.py'
CHECKS = {
    'openttd': lambda first, last: last['date'] > first['date'] and last['paused'] is False
    and len(last['companies']) >= 4 and first['session']['record'] == 'start',
    'wesnoth': lambda first, last: last['scenario'] == 'The Elves Besieged' and last['game_starts'] == 1
    and isinstance(last['turn'], int) and last['turn'] > first['turn'],
    'openra': lambda first, last: last['world']['world_tick'] > first['world']['world_tick']
    and len(last['world']['players']) == 4 and all(p['structures'] > 0 for p in last['world']['players'] if p['is_bot']),
    'speed-dreams': lambda first, last: last['sim']['t'] > first['sim']['t'] and len(last['standings']) == 10
    and last['session']['session'] == 'Qualifying',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('--env-dir', '--task', '--exporter', '--output'):
        parser.add_argument(name, required=True)
    parser.add_argument('--game', required=True, choices=sorted(CHECKS))
    parser.add_argument('--minutes', type=float, default=4)
    parser.add_argument('--interval', type=float, default=30)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    env = from_config(args.env_dir, task_id=args.task)
    env.env_spec.recording.output_dir = str(output / 'episode')
    reads, inputs, log = [], [], output / 'reads.jsonl'
    try:
        if args.game == 'wesnoth':
            env.reset(seed=42, use_cache=False)
        else:
            env.reset(seed=42, use_cache=True, cache_level='post_start')
        # A stopped game (save collector deadline, SIGSTOP) cannot advance; record the process states.
        inputs.append({'game_processes': env.runner.exec_capture(
            "ps -eo pid,stat,etime,comm | grep -E 'wesnoth|openttd|OpenRA|speed-dreams' || true")})
        env.runner.exec_capture('install -d -m 700 /run/sandweave-state')
        env.runner.copy_to(args.exporter, GUEST)
        argv = f'python3 {GUEST} {args.game}' + (' --resim' if args.game == 'openra' else '')
        plan = []
        if args.game == 'openttd':
            plan = [(10, [{'keyboard': {'keys': ['F1']}}], 'unpause (F1)')]
        if args.game == 'speed-dreams':
            # The task loads qualifying paused on the grid, waiting for Enter.
            plan = [(10, [{'keyboard': {'keys': ['Return']}}], 'start the session (Enter)')]
        if args.game == 'wesnoth':
            # Click the End Turn button (bottom right of the 1920x1080 screen), confirm "units have
            # movement left" with Enter, then the AI sides play their turn 1.
            plan = [(30, [{'mouse': {'left_click': [1830, 1052]}}], 'end turn (button)'),
                    (34, [{'keyboard': {'keys': ['Return']}}], 'confirm')]
        begin = time.monotonic()
        while time.monotonic() - begin < args.minutes * 60:
            elapsed = time.monotonic() - begin
            for when, actions, label in [p for p in plan if p[0] <= elapsed]:
                env.step(actions)
                inputs.append({'at_s': round(time.monotonic() - begin, 1), 'label': label, 'actions': actions})
                plan.remove((when, actions, label))
            observed = json.loads(env.runner.exec_capture(argv))
            observed['host_elapsed_s'] = round(time.monotonic() - begin, 1)
            reads.append(observed)
            with log.open('a') as handle:
                handle.write(json.dumps(observed) + '\n')
            env.runner.capture_screenshot(str(output / f'screen-{len(reads):02d}.png'))
            wait = min([p[0] for p in plan] + [elapsed + args.interval]) - (time.monotonic() - begin)
            time.sleep(max(0, wait))
        # The raw telemetry the reads came from (the newest replay for OpenRA).
        env.runner.copy_from(reads[-1]['source'], str(output / ('telemetry-' + Path(reads[-1]['source']).name)))
        first, last = reads[0]['state'], reads[-1]['state']
        summary = {'game': args.game, 'reads': len(reads), 'inputs': inputs, 'passed': bool(CHECKS[args.game](first, last)),
                   'guest_time_increasing': all(b['guest_ns'] > a['guest_ns'] for a, b in zip(reads, reads[1:])),
                   'first': first, 'last': last}
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps({key: summary[key] for key in ('game', 'reads', 'passed', 'guest_time_increasing')}))
    finally:
        env.close()


if __name__ == '__main__':
    main()
