#!/usr/bin/env python3
"""Check the SDL gamepad proxy and the persistent writer against a real SDL2 library (Linux).

Builds sdl-gamepad-proxy.c with the installer's flags and pinned dynapi header, and a
probe (sdl-gamepad-proxy-check.c) linked to the system libSDL2 that loads the proxy
through SDL_DYNAMIC_API. While the probe samples the joystick every 20 ms, this drives
gamepad-input.py --serve: a held button and axis must persist past the writer's 1 s
lease, a release must take effect while the axis stays set, end of input must leave
the pad neutral, and the probe's rumble requests must come back through the writer.

    scripts/sdl-gamepad-proxy-check.py --work DIR
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

HERE = Path(__file__).resolve().parent
HEADER_URL = 'https://raw.githubusercontent.com/libsdl-org/SDL/release-2.30.9/src/dynapi/SDL_dynapi_procs.h'
HEADER_SHA256 = 'b62e701d15d3f26760ce27015a2a8aaa3cf38424b9235b4a23b742063710893c'
MARGIN_NS = 50_000_000


def linked_sdl(program):
    """The SDL2 library file the probe actually loads."""
    for line in subprocess.run(['ldd', str(program)], capture_output=True, text=True, check=True).stdout.splitlines():
        if 'libSDL2-2.0.so.0' in line and '=>' in line:
            return os.path.realpath(line.split('=>')[1].split('(')[0].strip())
    raise SystemExit('the probe does not link libSDL2')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    work = args.work
    work.mkdir(parents=True, exist_ok=False)
    header = urllib.request.urlopen(HEADER_URL, timeout=30).read()
    if hashlib.sha256(header).hexdigest() != HEADER_SHA256:
        raise SystemExit('SDL dynapi header hash mismatch')
    (work / 'SDL_dynapi_procs.h').write_bytes(header)
    proxy, probe = work / 'libsdl-gamepad-proxy.so', work / 'probe'
    # The same flags as install-sandbox-vr.sh.
    subprocess.run(['cc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror', '-Wl,-Bsymbolic', '-I', str(work),
                    str(HERE / 'sdl-gamepad-proxy.c'), '-ldl', '-o', str(proxy)], check=True)
    subprocess.run(['cc', '-O2', '-Wall', '-Werror', str(HERE / 'sdl-gamepad-proxy-check.c'), '-o', str(probe),
                    '-l:libSDL2-2.0.so.0'], check=True)
    state, haptics = work / 'pad.bin', work / 'haptics.bin'
    writer = subprocess.Popen([sys.executable, '-u', str(HERE / 'gamepad-input.py'), '--serve', '--state', str(state),
                               '--haptics', str(haptics)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def request(value):
        writer.stdin.write((json.dumps(value) + '\n').encode())
        writer.stdin.flush()
        reply = json.loads(writer.stdout.readline())
        if 'error' in reply:
            raise RuntimeError('writer rejected ' + json.dumps(value) + ': ' + reply['error'])
        return reply

    if not json.loads(writer.stdout.readline()).get('ready'):
        raise SystemExit('writer did not start')
    environment = {**os.environ, 'SDL_DYNAMIC_API': str(proxy), 'LAB_GAMEPAD_STATE': str(state),
                   'LAB_GAMEPAD_HAPTICS': str(haptics)}
    with (work / 'probe.stderr').open('wb') as errors:
        sampler = subprocess.Popen([str(probe), '7'], env=environment, stdout=subprocess.PIPE, stderr=errors)
        started = json.loads(sampler.stdout.readline())
        begin = time.monotonic()

        def at(seconds):
            time.sleep(max(0, begin + seconds - time.monotonic()))

        at(.5)
        held = request({'buttons_down': ['a'], 'axes': {'lx': .5}})
        at(3.5)
        released = request({'buttons_up': ['a']})
        at(4.5)
        rumble = request({'haptics_after': 0})
        at(5)
        writer.stdin.close()
        closed_ns = time.monotonic_ns()
        writer.wait(timeout=5)
        samples = [json.loads(line) for line in sampler.stdout]
        sampler.wait(timeout=10)
    if sampler.returncode:
        raise SystemExit('probe failed: ' + (work / 'probe.stderr').read_text()[-2000:])
    half = round(.5 * 32767)

    def window(start, end):
        return [s for s in samples if start + MARGIN_NS <= s['t_ns'] <= end - MARGIN_NS]

    held_samples = window(held['guest_ack_ns'], released['guest_ack_ns'])
    released_samples = window(released['guest_ack_ns'], closed_ns)
    neutral_samples = [s for s in samples if s['t_ns'] >= closed_ns + MARGIN_NS]
    expected_rumble = [('motors', 1.0, 0x8000 / 0xFFFF, 250, False), ('triggers', 0x4000 / 0xFFFF, 0.0, 100, False),
                       ('motors', 0.0, 0.0, 0, True)]
    observed_rumble = [(e['output'], e.get('low_frequency', e.get('left_trigger')),
                        e.get('high_frequency', e.get('right_trigger')), e['duration_ms'], e['stop'])
                       for e in rumble['events']]
    checks = {
        'proxy_joystick': started['joysticks'] == 1 and started['name'] == 'Lab Virtual Gamepad',
        'rumble_advertised': started['has_rumble'] == 1 and started['has_rumble_triggers'] == 1,
        'rumble_calls_succeeded': started['rumble'] == started['rumble_triggers'] == started['rumble_stop'] == 0,
        'rumble_recorded': observed_rumble == expected_rumble,
        'rumble_stamped_after_request': all(e['guest_ns'] >= started['requested_ns'] for e in rumble['events']),
        'held_beyond_lease': len(held_samples) > 0 and (held_samples[-1]['t_ns'] - held_samples[0]['t_ns']) > 2e9
        and all(s['buttons'] == 1 and s['axes'][0] == half for s in held_samples),
        'release_keeps_axis': len(released_samples) > 0 and all(s['buttons'] == 0 and s['axes'][0] == half
                                                               for s in released_samples),
        'neutral_after_end': len(neutral_samples) > 0 and all(s['buttons'] == 0 and not any(s['axes'])
                                                             for s in neutral_samples)}
    summary = {'passed': all(checks.values()), 'checks': checks, 'probe': started,
               'samples': {'total': len(samples), 'held': len(held_samples), 'released': len(released_samples),
                           'neutral': len(neutral_samples)},
               'held_seconds': (held_samples[-1]['t_ns'] - held_samples[0]['t_ns']) / 1e9 if held_samples else 0,
               'rumble_events': rumble['events'], 'sdl': linked_sdl(probe)}
    (work / 'samples.jsonl').write_text(''.join(json.dumps(s) + '\n' for s in samples))
    (work / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
