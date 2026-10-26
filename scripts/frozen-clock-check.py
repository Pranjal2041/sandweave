#!/usr/bin/env python3
"""Check game time on a frozen guest clock (runtime="gvisor", clock_pause="freeze").

    frozen-clock-check.py --output DIR

A GNOME sandbox with clock_pause="freeze" runs a guest logger that records its
CLOCK_MONOTONIC every 50 ms, as a game's frame loop reads it. Unified steps then
pause (game_speed 0) for 4 s of host time, advance 1000 ms of game time from the
pause (wait), stay paused 3 s, click and capture a frame while paused (the guest
runs only for each), and resume. Checks: the guest never observes a jump (largest
step between log lines), its total elapsed time equals host time minus the paused
time, and the wait advanced about 1000 ms of guest time.
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

        step('pause', [{'kind': 'game_speed', 'scale': 0}])
        time.sleep(4)
        step('advance-1000ms', [{'kind': 'wait', 'ms': 1000}])
        time.sleep(3)
        step('click-and-frame-while-paused', [{'kind': 'click', 'x': 100, 'y': 100}], observe=['frame'])
        time.sleep(2)
        step('resume', [{'kind': 'game_speed', 'scale': 1}])
        time.sleep(2)
        host['end'] = time.monotonic()
        logger.terminate()
        guest = [int(line) for line in sandbox.files.read_text('/tmp/clock.log').split()]
        info_clock = sandbox.info.get('clock')
    # Host intervals in which the guest was certainly paused: from the end of the 'pause' step to the
    # start of 'resume', less the whole wait and delivery steps (each resumes and pauses inside).
    duration = lambda index: steps[index]['host_end_s'] - steps[index]['host_begin_s']  # noqa: E731
    paused_host = steps[3]['host_begin_s'] - steps[0]['host_end_s'] - duration(1) - duration(2)
    ran_host = steps[1]['acks'][0]['host_running_ms'] / 1000
    first = 0
    gaps = [(b - a) / 1e6 for a, b in zip(guest, guest[1:])]
    host_total = host['end'] - host['start']
    guest_total = (guest[-1] - guest[first]) / 1e9
    summary = {'clock': info_clock, 'host_total_s': round(host_total, 3), 'host_paused_s': round(paused_host, 3),
               'wait_host_running_ms': round(ran_host * 1000, 1), 'guest_log_lines': len(guest),
               'guest_total_s': round(guest_total, 3), 'largest_guest_step_ms': round(max(gaps), 1),
               'steps': steps}
    # The logger began 2 s before host['start'] and stopped 2 s after resuming: the guest can have run
    # at most that plus the host time outside the certainly-paused intervals.
    expected_guest = 2 + host_total - paused_host
    summary.update(expected_guest_s_upper=round(expected_guest, 3), wait_step_host_s=round(duration(1), 3),
                   delivery_host_s=round(duration(2), 3), guest_time_hidden_s=round(2 + host_total - guest_total, 3))
    summary['checks'] = {
        'no_observed_jump': max(gaps) < 250,
        'guest_time_excludes_pauses': guest_total <= expected_guest,
        'paused_long_enough_to_tell': paused_host > 6,
        'wait_advanced_about_1s': 990 <= steps[1]['acks'][0]['host_running_ms'] <= 1300,
        'frame_captured_while_paused': steps[2]['frame_size'] is not None,
    }
    summary['passed'] = all(summary['checks'].values())
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    (args.output / 'guest-clock.log').write_text('\n'.join(map(str, guest)) + '\n')
    print(json.dumps({key: summary[key] for key in ('passed', 'checks', 'host_total_s', 'host_paused_s', 'guest_total_s',
                                                     'expected_guest_s_upper', 'delivery_host_s', 'guest_time_hidden_s',
                                                     'largest_guest_step_ms', 'wait_host_running_ms')}))


if __name__ == '__main__':
    main()
