#!/usr/bin/env python3
"""Does fast I/O end a Wesnoth turn? (worker with gym-anything and the Wesnoth frontier environment)

    wesnoth-input-check.py --env-dir DIR --task TASK --exporter state_export.py --output DIR

Starts the task as its harness does (reset(seed=42, use_cache=False)), then clicks
End Turn only through gym-anything's Sandweave runner (fast I/O), as the evaluation's
solver did: one click, a wait, and if the turn has not changed a second click and
Enter. Before and after each input it records the focused X window and the pointer
(xdotool queries; no input) and the game's turn from Sandweave's state exporter.
"""
import argparse
import json
from pathlib import Path
import time

from gym_anything.api import from_config

GUEST = '/run/sandweave-state/state_export.py'
QUERY = 'env DISPLAY=:1 XAUTHORITY=/home/ga/.Xauthority xdotool'
END_TURN = [1830, 1052]  # the End Turn button on the 1920x1080 screen (the solver's clicks)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('--env-dir', '--task', '--exporter', '--output'):
        parser.add_argument(name, required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    env = from_config(args.env_dir, task_id=args.task)
    env.env_spec.recording.output_dir = str(output / 'episode')
    log = []
    try:
        env.reset(seed=42, use_cache=False)
        env.runner.exec_capture('install -d -m 700 /run/sandweave-state')
        env.runner.copy_to(args.exporter, GUEST)

        def observe(label):
            state = json.loads(env.runner.exec_capture(f'python3 {GUEST} wesnoth'))['state']
            focus = env.runner.exec_capture(f'{QUERY} getwindowfocus getwindowname 2>&1 || true').strip()
            pointer = env.runner.exec_capture(f'{QUERY} getmouselocation 2>&1 || true').strip()
            entry = {'label': label, 't': round(time.monotonic() - begin, 1), 'turn': state['turn'],
                     'events': state['events'], 'focus': focus, 'pointer': pointer}
            log.append(entry)
            env.runner.capture_screenshot(str(output / f'{len(log):02d}-{label}.png'))
            print(json.dumps(entry), flush=True)
            return state['turn']

        begin = time.monotonic()
        start = observe('hand-over')
        env.step([{'mouse': {'move': [960, 540]}}])
        time.sleep(2)
        observe('after-move')
        env.step([{'mouse': {'left_click': END_TURN}}])
        time.sleep(25)
        first = observe('after-click-1')
        if first == start:
            env.step([{'mouse': {'left_click': END_TURN}}])
            time.sleep(4)
            observe('after-click-2')
            env.step([{'keyboard': {'keys': ['Return']}}])
            time.sleep(30)
            observe('after-enter')
        result = {'start_turn': start, 'final_turn': log[-1]['turn'], 'ended_by_fast_io': log[-1]['turn'] != start,
                  'first_click_ended_turn': first != start, 'log': log}
        (output / 'summary.json').write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps({key: result[key] for key in ('start_turn', 'final_turn', 'ended_by_fast_io',
                                                         'first_click_ended_turn')}))
    finally:
        env.close()


if __name__ == '__main__':
    main()
