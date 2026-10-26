#!/usr/bin/env python3
"""Game time in a real game: OpenTTD on a frozen guest clock (frontier worker).

    openttd-game-time-check.py --env-dir DIR --task TASK --exporter state_export.py --output DIR

Needs gym-anything's Sandweave runner to create the sandbox with clock_pause="freeze"
(GYM_SANDWEAVE_CLOCK_PAUSE=freeze). OpenTTD logs a GACR record at every month start
(a day is 74 ticks of 27 ms). After unpausing, the check waits for a new month, then:
freezes for 150 s of host time and thaws (the date must not move); freezes and
advances exactly 150000 ms of game time, 75.1 game days, and thaws (the date must
move exactly two months, as 75 days span two month starts from the 1st of any month).
"""
import argparse
from datetime import date
import json
from pathlib import Path
import time

from gym_anything.api import from_config
from sandweave.steps import run as run_step

GUEST = '/run/sandweave-state/state_export.py'


def months(text):
    day = date.fromisoformat(text)
    return day.year * 12 + day.month - 1, day


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
        sandbox = env.runner._sandbox
        assert (sandbox.info.get('clock') or {}).get('pause') == 'freeze', sandbox.info.get('clock')
        env.runner.exec_capture('install -d -m 700 /run/sandweave-state')
        env.runner.copy_to(args.exporter, GUEST)

        def read(label):
            state = json.loads(env.runner.exec_capture(f'python3 {GUEST} openttd'))['state']
            uptime = float(env.runner.exec_capture('cat /proc/uptime').split()[0])
            entry = {'label': label, 'host_s': round(time.monotonic() - begin, 2), 'date': state['date'],
                     'paused': state['paused'], 'guest_uptime_s': uptime}
            log.append(entry)
            print(json.dumps(entry), flush=True)
            return entry

        def step(actions):
            return run_step(sandbox, {'actions': actions, 'observe': []})['acknowledgements']

        begin = time.monotonic()
        env.step([{'keyboard': {'keys': ['F1']}}])  # the task starts paused
        first = read('unpaused')
        while True:  # a month start: the date just changed
            time.sleep(.5)
            start = read('waiting')
            if start['date'] != first['date']:
                break
            if time.monotonic() - begin > 180:
                raise RuntimeError('the OpenTTD date did not advance after unpausing')
        step([{'kind': 'game_speed', 'scale': 0}])
        time.sleep(150)
        step([{'kind': 'game_speed', 'scale': 1}])
        after_freeze = read('after 150 s frozen')
        step([{'kind': 'game_speed', 'scale': 0}])
        advance = step([{'kind': 'wait', 'ms': 150000}])[0]
        step([{'kind': 'game_speed', 'scale': 1}])
        after_advance = read('after advancing 150000 ms')
        m0, _ = months(start['date'])
        m1, _ = months(after_freeze['date'])
        m2, _ = months(after_advance['date'])
        checks = {
            'frozen_date_unchanged': after_freeze['date'] == start['date'],
            'guest_did_not_see_the_freeze': after_freeze['guest_uptime_s'] - start['guest_uptime_s'] < 30,
            'advance_exact': advance['advanced_ms'] == 150000.0,
            'advance_moved_two_months': m2 - m1 == 2,
        }
        summary = {'passed': all(checks.values()), 'checks': checks, 'advance': advance, 'month_deltas': [m1 - m0, m2 - m1],
                   'log': log}
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps({key: summary[key] for key in ('passed', 'checks', 'advance', 'month_deltas')}))
    finally:
        env.close()


if __name__ == '__main__':
    main()
