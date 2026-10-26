#!/usr/bin/env python3
"""Drive Speed Dreams with Sandweave's virtual gamepad and record its force feedback.

Runs on a worker with gym-anything and the Speed Dreams frontier environment:

    gamepad-speed-dreams-check.py --env-dir DIR --task TASK --scripts SANDWEAVE/scripts \
        --header SDL_dynapi_procs.h --output DIR

Starts the task with gym_anything (the qualifying session loads paused on the grid),
then restarts the game exactly as the task launches it, plus the SDL gamepad proxy
(SDL_DYNAMIC_API) and the player's throttle, brake and steering on the pad (right
trigger, left trigger, left stick X; automatic gears). One gamepad-input.py --serve
writer holds the pad state. The car's own commands and speed come back through
Sandweave's state exporter (race journal 'h' records: ac, br, st, v) through phases:
neutral, throttle (rt=1), brake (lt=1), throttle with steering left (lx=-0.6), release.
The force feedback the game sends to the pad (SDL_Haptic rumble at start-up and the
constant force while steering is on the pad) is read back through the writer.
"""
import argparse
import json
from pathlib import Path
import re
import statistics
import time

from gym_anything.api import from_config

GUEST = '/opt/sandweave-gamepad'
STATE, HAPTICS = '/tmp/lab-gamepad.bin', '/tmp/lab-gamepad-haptics.bin'
REQUESTS, REPLIES = '/run/sandweave-gamepad.requests', '/run/sandweave-gamepad.replies'
PREFS = '/var/lib/sdracer/sd2/drivers/human/preferences.xml'
DRIVER_SETTINGS = [  # (element, name, value) for the player's section
    ('attstr', 'transmission', 'auto'),
    ('attstr', 'throttle', 'AXIS5-0'), ('attnum', 'throttle min', '-1'), ('attnum', 'throttle max', '1'),
    ('attnum', 'throttle power', '1'), ('attnum', 'throttle sensitivity', '1'),
    ('attstr', 'brake', 'AXIS2-0'), ('attnum', 'brake min', '-1'), ('attnum', 'brake max', '1'),
    ('attnum', 'brake power', '1'), ('attnum', 'brake sensitivity', '1'),
    # Left steer grows as the stick goes left (negative power flips its polarity), right steer as it goes right.
    ('attstr', 'left steer', 'AXIS0-0'), ('attnum', 'left steer min', '-1'), ('attnum', 'left steer max', '0'),
    ('attnum', 'left steer power', '-1'),
    ('attstr', 'right steer', 'AXIS0-0'), ('attnum', 'right steer min', '0'), ('attnum', 'right steer max', '1'),
    ('attnum', 'right steer power', '1'), ('attnum', 'steer sensitivity', '1'),
]
PHASES = [('neutral', 4, {'axes': {'rt': 0, 'lt': 0, 'lx': 0}}), ('throttle', 8, {'axes': {'rt': 1}}),
          ('brake', 4, {'axes': {'rt': 0, 'lt': 1}}), ('steer_left', 4, {'axes': {'lt': 0, 'rt': 1, 'lx': -.6}}),
          ('release', 3, {'axes': {'rt': 0, 'lt': 0, 'lx': 0}})]


def player_preferences(text):
    """The player's section (Preferences/Drivers/1) with the pad settings."""
    start = text.index('<section name="Drivers">')
    begin = text.index('<section name="1">', start)
    end = text.index('</section>', begin)
    block = text[begin:end]
    for element, name, value in DRIVER_SETTINGS:
        pattern = re.compile(rf'<att(str|num) name="{re.escape(name)}"[^>]*/>')
        line = f'<{element} name="{name}" val="{value}"/>'
        block = pattern.sub(line, block, count=1) if pattern.search(block) else block + f'  {line}\n      '
    return text[:begin] + block + text[end:]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('--env-dir', '--task', '--scripts', '--header', '--exporter', '--output'):
        parser.add_argument(name, required=True)
    args = parser.parse_args()
    output, scripts = Path(args.output), Path(args.scripts)
    output.mkdir(parents=True, exist_ok=False)
    env = from_config(args.env_dir, task_id=args.task)
    env.env_spec.recording.output_dir = str(output / 'episode')
    run = env.runner.exec_capture
    try:
        env.reset(seed=42, use_cache=True, cache_level='post_start')
        run('source /workspace/scripts/sd_env.sh && sd_stop_game')
        run(f'install -d -m 755 {GUEST}')
        for source, name in ((scripts / 'sdl-gamepad-proxy.c', 'sdl-gamepad-proxy.c'), (Path(args.header), 'SDL_dynapi_procs.h'),
                             (scripts / 'gamepad-input.py', 'gamepad-input.py'), (Path(args.exporter), 'state_export.py')):
            env.runner.copy_to(str(source), f'{GUEST}/{name}')
        # The installer's flags (install-sandbox-vr.sh).
        run(f'cc -shared -fPIC -O2 -Wall -Wextra -Werror -Wl,-Bsymbolic -I {GUEST} {GUEST}/sdl-gamepad-proxy.c -ldl '
            f'-o {GUEST}/libsdl-gamepad-proxy.so && chmod 755 {GUEST}/libsdl-gamepad-proxy.so {GUEST}/gamepad-input.py')
        env.runner.copy_from(PREFS, str(output / 'preferences.original.xml'))
        (output / 'preferences.xml').write_text(player_preferences((output / 'preferences.original.xml').read_text()))
        env.runner.copy_to(str(output / 'preferences.xml'), PREFS)
        run(f'chown sdracer:sdracer {PREFS} && rm -f {STATE} {HAPTICS} && install -o sdracer -g sdracer -m 600 /dev/null {HAPTICS}'
            f' && : > {REQUESTS} && : > {REPLIES}')
        # One persistent writer as the game's user; requests are lines appended to REQUESTS.
        run(f"(setsid sh -c 'tail -n +1 -f {REQUESTS} | runuser -u sdracer -- python3 -u {GUEST}/gamepad-input.py --serve "
            f"--state {STATE} --haptics {HAPTICS} > {REPLIES} 2> {REPLIES}.stderr' > /dev/null 2>&1 < /dev/null &)")
        deadline = time.monotonic() + 20
        while '"ready"' not in run(f'cat {REPLIES}'):
            if time.monotonic() > deadline:
                raise RuntimeError('the gamepad writer did not start: ' + run(
                    f'cat {REPLIES} {REPLIES}.stderr; ps -eo pid,user,args | grep -e [g]amepad-input -e "[t]ail -n"; true'))
            time.sleep(.2)
        replies = [0]

        def writer(request):
            run(f"echo '{json.dumps(request)}' >> {REQUESTS}")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                lines = run(f'cat {REPLIES}').splitlines()
                if len(lines) > replies[0] + 1:  # the first line is the ready message
                    replies[0] += 1
                    return json.loads(lines[replies[0]])
                time.sleep(.05)
            raise TimeoutError('the gamepad writer did not reply to ' + json.dumps(request) + ': ' + run(
                f'cat {REPLIES} {REPLIES}.stderr; ps -eo pid,user,args | grep -e [g]amepad-input -e "[t]ail -n"; true'))

        game_log = '/var/lib/sdracer/eval/game.gamepad.log'
        run('source /workspace/scripts/sd_env.sh && install -o "$SD_USER" -g "$SD_USER" -m 600 /home/ga/.Xauthority '
            '"$SD_HOME/.Xauthority" && (env -i HOME="$SD_HOME" USER="$SD_USER" LOGNAME="$SD_USER" PATH=/usr/bin:/bin '
            'DISPLAY=:1 XAUTHORITY="$SD_HOME/.Xauthority" LD_LIBRARY_PATH="$SD_LIB" SD_EVAL_JOURNAL="$SD_JOURNAL" '
            f'SDL_DYNAMIC_API={GUEST}/libsdl-gamepad-proxy.so LAB_GAMEPAD_STATE={STATE} LAB_GAMEPAD_HAPTICS={HAPTICS} '
            f'/usr/sbin/runuser -u "$SD_USER" -- "$SD_BIN" -lc "$SD_LOCAL" -s "$RACEMAN" > {game_log} 2>&1 &)')
        deadline = time.monotonic() + 240
        while 'PRE RACE PAUSE state' not in run(f'cat {game_log}'):
            if time.monotonic() > deadline:
                raise TimeoutError('Speed Dreams did not reach the qualifying grid')
            time.sleep(2)
        time.sleep(3)
        run('xdotool search --name "Speed Dreams" windowactivate --sync key Return')  # start the session
        time.sleep(3)
        exporter = f'python3 {GUEST}/state_export.py speed-dreams --cache /run/sandweave-gamepad-state'
        reads, applied = [], []
        for phase, seconds, update in PHASES:
            applied.append({'phase': phase, **writer(update)})
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                observed = json.loads(run(exporter))
                car = (observed['state']['humans'] or [{}])[0]
                reads.append({'phase': phase, 'guest_ns': observed['guest_ns'], 'sim_t': observed['state']['sim']['t'],
                              **{key: car.get(key) for key in ('ac', 'br', 'st', 'v', 'g', 'lap', 'pos')}})
                time.sleep(.4)
        haptics = writer({'haptics_after': 0})
        env.runner.capture_screenshot(str(output / 'screen.png'))
        env.runner.copy_from(game_log, str(output / 'game.log'))
        log = (output / 'game.log').read_text(errors='replace')

        def phase(name, key):
            values = [r[key] for r in reads if r['phase'] == name and r[key] is not None]
            return values[len(values) // 3:]  # after the commands settle

        median = {(name, key): statistics.median(phase(name, key)) for name, _, _ in PHASES for key in ('ac', 'br', 'st')}
        speed = {name: (phase(name, 'v')[0], phase(name, 'v')[-1]) for name, _, _ in PHASES}
        constant = [e for e in haptics['events'] if e['output'] == 'constant_force']
        checks = {
            'proxy_loaded': 'lab-gamepad: SDL ABI' in log and 'Lab Virtual Gamepad' in log,
            'neutral_no_commands': median[('neutral', 'ac')] <= .02 and median[('neutral', 'br')] <= .02,
            'throttle_follows_trigger': median[('throttle', 'ac')] >= .98 and median[('throttle', 'br')] <= .02,
            'car_accelerates': speed['throttle'][1] - speed['throttle'][0] > 5,
            'brake_follows_trigger': median[('brake', 'br')] >= .98 and median[('brake', 'ac')] <= .02,
            'car_slows_under_brake': speed['brake'][1] < speed['brake'][0],
            'steer_follows_stick': median[('steer_left', 'st')] > .3,
            'release_clears': median[('release', 'ac')] <= .02 and median[('release', 'br')] <= .02
            and abs(median[('release', 'st')]) <= .02,
            'startup_rumble_recorded': any(e['output'] == 'haptic_rumble' and abs(e['strength'] - .5) < .01
                                           for e in haptics['events']),
            'constant_force_while_steering_on_pad': any(abs(e['level']) > 0 for e in constant),
        }
        summary = {'passed': all(checks.values()), 'checks': checks, 'medians': {f'{p}/{k}': v for (p, k), v in median.items()},
                   'speed_m_s': speed, 'reads': len(reads), 'haptic_events': len(haptics['events']),
                   'constant_force_events': len(constant),
                   'constant_force_levels': sorted({round(e['level'], 3) for e in constant})[:20],
                   'rumble_events': [e for e in haptics['events'] if e['output'] != 'constant_force'][:10], 'writer': applied}
        (output / 'reads.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in reads))
        (output / 'haptics.json').write_text(json.dumps(haptics, indent=1) + '\n')
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(json.dumps({key: summary[key] for key in ('passed', 'checks', 'speed_m_s', 'haptic_events')}))
    finally:
        env.close()


if __name__ == '__main__':
    main()
