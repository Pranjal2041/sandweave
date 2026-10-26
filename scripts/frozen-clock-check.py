#!/usr/bin/env python3
"""Check game time on a frozen guest clock (runtime="gvisor", clock_pause="freeze").

    frozen-clock-check.py --output DIR

A GNOME sandbox with clock_pause="freeze" runs a guest logger that records its
CLOCK_MONOTONIC every 50 ms, as a game's frame loop reads it. Unified steps freeze
it (game_speed 0) for 4 s of host time, click and observe a frame while frozen,
wait 2 s, advance exactly 1000 ms of game time (wait), stay frozen 3 s and thaw.
Checks: the engine advanced exactly 1000 ms; the click was queued and delivered in
the advance; the frame came from the freeze; the guest never observed a jump; and
its elapsed time equals its running time (bounded by the freeze/thaw round trips).
"""
import argparse
import json
from pathlib import Path
import time

from sandweave import Sandbox
from sandweave.steps import run as run_step

LOGGER = ("import time\nwith open('/tmp/clock.log', 'w') as log:\n    while True:\n"
          "        log.write(f'{time.monotonic_ns()}\\n'); log.flush(); time.sleep(.05)\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    with Sandbox(template='gnome', clock_pause='freeze') as sandbox:
        sandbox.files.write_text('/tmp/logger.py', LOGGER)
        logger = sandbox.exec('exec python3 -u /tmp/logger.py')
        time.sleep(2)
        host = {'start': time.monotonic()}
        steps = []

        def step(name, actions, observe=()):
            began = time.monotonic()
            result = run_step(sandbox, {'id': name, 'actions': actions, 'observe': list(observe)})
            steps.append({'step': name, 'host_begin_s': began - host['start'], 'host_end_s': time.monotonic() - host['start'],
                          'acks': result['acknowledgements'],
                          'frame_size': list(result['observations']['frame']['value'].size)
                          if 'frame' in result['observations'] else None})

        step('freeze', [{'kind': 'game_speed', 'scale': 0}])
        time.sleep(4)
        step('click-and-frame-while-frozen', [{'kind': 'click', 'x': 100, 'y': 100}], observe=['frame'])
        time.sleep(2)
        step('advance-1000ms', [{'kind': 'wait', 'ms': 1000}])
        time.sleep(3)
        step('thaw', [{'kind': 'game_speed', 'scale': 1}])
        time.sleep(2)
        host['end'] = time.monotonic()
        logger.terminate()
        guest = [int(line) for line in sandbox.files.read_text('/tmp/clock.log').split()]
        info_clock = sandbox.info.get('clock')
    gaps = [(b - a) / 1e6 for a, b in zip(guest, guest[1:])]
    advance = steps[2]['acks'][0]
    # Guest time ran: 2 s before host['start'], until the freeze, exactly the advance, and from the thaw.
    # Freeze and thaw are host round trips, so they bound the comparison, not the advance.
    running = 2 + steps[0]['host_end_s'] + advance['advanced_ms'] / 1000 + (host['end'] - host['start'] - steps[3]['host_begin_s'])
    slack = (steps[0]['host_end_s'] - steps[0]['host_begin_s']) + (steps[3]['host_end_s'] - steps[3]['host_begin_s'])
    guest_total = (guest[-1] - guest[0]) / 1e9
    summary = {'clock': info_clock, 'host_total_s': round(host['end'] - host['start'] + 2, 3), 'guest_total_s': round(guest_total, 3),
               'guest_expected_s': round(running, 3), 'freeze_thaw_round_trips_s': round(slack, 3),
               'advance': advance, 'largest_guest_step_ms': round(max(gaps), 1), 'steps': steps}
    summary['checks'] = {
        'no_observed_jump': max(gaps) < 250,
        'advance_exact': advance['advanced_ms'] == 1000.0,
        'input_queued_while_frozen': steps[1]['acks'][0] == {'queued_until_game_time_runs': True},
        'queued_input_delivered_in_advance': advance['delivered'] == 1,
        'frozen_frame_served': steps[1]['frame_size'] is not None,
        'guest_time_matches': abs(guest_total - running) <= slack + 0.06,
    }
    summary['passed'] = all(summary['checks'].values())
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    (args.output / 'guest-clock.log').write_text('\n'.join(map(str, guest)) + '\n')
    print(json.dumps({key: summary[key] for key in ('passed', 'checks', 'host_total_s', 'guest_total_s', 'guest_expected_s',
                                                     'freeze_thaw_round_trips_s', 'advance', 'largest_guest_step_ms')}))


if __name__ == '__main__':
    main()
