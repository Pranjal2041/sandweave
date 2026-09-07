#!/usr/bin/env python3
"""Acceptance/latency tests against an explicitly selected disposable fastio-* desktop.

Install fast-io-input-probe.py as a systemd service before running this test.
The GPU variant uses the probe's /gpu page in an accelerated Firefox window.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import statistics
import time
import urllib.request
import uuid

from environment import EnvironmentManager
import environment_control as control
from fast_io import FastIOClient, detach


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name')
    parser.add_argument('--gpu', action='store_true')
    parser.add_argument('--lifecycle', action='store_true')
    args = parser.parse_args()
    if not args.name.startswith('fastio-'):
        parser.error('use a disposable environment named fastio-*')
    manager = EnvironmentManager()
    client = manager.fast_io(args.name)
    report = {'name': args.name, 'gpu': args.gpu, 'iterations': 200}
    output = manager.lab / 'runs/fastio-acceptance' / args.name
    output.mkdir(parents=True, exist_ok=True)

    def state(name=args.name):
        port = manager.status(name)['ports']['8000']
        with urllib.request.urlopen(f'http://127.0.0.1:{port}', timeout=3) as response:
            return json.load(response)

    def wait(predicate, timeout=10):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                value = predicate()
                if value:
                    return value
            except (OSError, RuntimeError) as error:
                last = error
            time.sleep(.005)
        raise TimeoutError('application acceptance timed out: ' + str(last))

    def pixel_matches(image, color):
        return all(abs(a-b) <= 1 for a,b in zip(image.getpixel((400,400)), color))

    def summary(values):
        return {'median_ms': statistics.median(values), 'p95_ms': sorted(values)[int(len(values)*.95)-1],
                'max_ms': max(values)}

    def guest(*command):
        return manager._run([*manager._command(args.name), 'exec', '--env=DISPLAY=:1',
                             '--env=XAUTHORITY=/home/ga/.Xauthority', args.name, *command])

    guest('sh', '-c', 'test ! -e /dev/kvm')
    client.action({'keyboard': {'keys': 'escape'}})
    if not args.gpu:
        client.action({'mouse': {'left_click': [150,190]}})
        client.action({'keyboard': {'keys': ['ctrl','a']}})
        expected = 'Fast IO! café Ω中'
        client.action({'keyboard': {'text': expected}})
        wait(lambda: state()['text'] == expected)
        prior = state()
        client.action([{'keyboard': {'keys_down': 'ctrl'}},
                       {'mouse': {'left_click_drag': [[200,400],[700,500]]}},
                       {'keyboard': {'keys_up': 'ctrl'}}])
        wait(lambda: state()['clicks'] > prior['clicks'])
        # A fast drag may coalesce intermediate motion. Its end must reach the
        # application with Control and button 1 held, followed by release.
        wait(lambda: any(e['type'] == 'button-release' and e.get('x') == 590 and e['state'] & 260 == 260
                         for e in state()['events']))
        client.action({'mouse': {'double_click': [300,450]}})
        client.action({'mouse': {'triple_click': [600,450]}})
        client.action({'mouse': {'scroll': 3}})
        wait(lambda: state()['clicks'] == prior['clicks'] + 6)
        wait(lambda: sum(e['type'] == 'scroll' for e in state()['events']) >= 3)
        before_bad = state()['clicks']
        try:
            client.action([{'mouse': {'left_click': [300,400]}}, {'mouse': {'move': [30000,30000]}}])
        except RuntimeError as error:
            assert 'outside desktop' in str(error)
        else:
            raise AssertionError('invalid batch was accepted')
        assert state()['clicks'] == before_bad
        report['input'] = {'text': expected, 'modifier_drag': True, 'double_triple_click': True,
                           'scroll': True, 'invalid_batch_atomic': True}
    else:
        gpu = wait(lambda: state().get('gpu'))
        assert 'NVIDIA' in gpu['renderer'], gpu
        report['renderer'] = gpu['renderer']

    color = state()['gpu' if args.gpu else 'color']
    if args.gpu:
        color = color['color']
    wait(lambda: pixel_matches(client.screenshot(), color))
    immediate_matches = 0
    paint_times = []
    for _ in range(30):
        before = state()['gpu'] if args.gpu else state()
        expected_color = [180,60,30] if before['clicks'] % 2 == 0 else [30,90,180]
        tick = time.perf_counter_ns()
        im, ack = client.step({'mouse': {'left_click': [400,400]}})
        assert client.last_metadata['frame_input_sequence'] >= ack['input_sequence']
        immediate_matches += pixel_matches(im, expected_color)
        wait(lambda: pixel_matches(client.screenshot(), expected_color))
        paint_times.append((time.perf_counter_ns()-tick)/1e6)
        wait(lambda: (state()['gpu'] if args.gpu else state())['clicks'] == before['clicks']+1)
    report['action_to_visible_pixel'] = {**summary(paint_times), 'first_capture_updated': immediate_matches, 'samples': 30}
    client.screenshot().save(output / 'verified.png')
    original = client.screenshot(); original_bytes = original.tobytes()
    first_sequence = client.last_metadata['frame_sequence']
    client.screenshot()
    assert client.last_metadata['frame_sequence'] > first_sequence
    assert original.tobytes() == original_bytes
    latest = client.screenshot(fresh=False)
    cached_seq = client.last_metadata['frame_sequence']
    client.screenshot(fresh=False)
    assert client.last_metadata['frame_sequence'] == cached_seq
    report['owned_images_and_freshness'] = True

    report['benchmarks'] = {}
    for mode in ('1280x800', '1920x1080'):
        guest('xrandr', '--output', 'VNC-0', '--mode', mode)
        dimensions = tuple(map(int, mode.split('x')))
        wait(lambda: client.screenshot().size == dimensions)
        time.sleep(.2)
        for _ in range(10):
            client.screenshot()
        groups = {}
        for operation in ('action', 'fresh_screenshot', 'latest_screenshot', 'step'):
            elapsed, capture, actions = [], [], []
            for i in range(200):
                action = {'mouse': {'move': [450+i%40,430]}}
                tick = time.perf_counter_ns()
                if operation == 'action':
                    metadata = client.action(action)
                elif operation == 'step':
                    _, metadata = client.step(action)
                else:
                    client.screenshot(fresh=operation == 'fresh_screenshot')
                    metadata = client.last_metadata
                elapsed.append((time.perf_counter_ns()-tick)/1e6)
                capture.append((metadata['capture_completed_ns']-metadata['capture_started_ns'])/1e6)
                actions.append(metadata['action_ns']/1e6)
            groups[operation] = summary(elapsed)
            if operation in ('fresh_screenshot', 'step'):
                groups[operation]['guest_capture'] = summary(capture)
            if operation in ('action', 'step'):
                groups[operation]['guest_action'] = summary(actions)
        report['benchmarks'][mode] = groups
    guest('xrandr', '--output', 'VNC-0', '--mode', '1280x800')
    wait(lambda: client.screenshot().size == (1280,800))

    def concurrent_read(_):
        with manager.fast_io(args.name) as peer:
            return all(peer.screenshot().size == (1280,800) for _ in range(10))
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(concurrent_read, range(2)))
    report['concurrent_readers'] = True

    if args.lifecycle:
        def control_is_held():
            code = ('import ctypes as C; x=C.CDLL("libX11.so.6"); '
                    'x.XOpenDisplay.restype=C.c_void_p; d=x.XOpenDisplay(None); '
                    'assert d; keys=C.create_string_buffer(32); '
                    'x.XQueryKeymap.argtypes=[C.c_void_p,C.c_void_p]; '
                    'x.XQueryKeymap(d,keys); print(bool(keys.raw[37//8] & (1<<(37%8))))')
            return guest('python3', '-c', code).strip() == 'True'

        before = state()
        generation = client.last_metadata['generation']
        client.action({'keyboard': {'keys_down': 'ctrl'}})
        assert control_is_held()
        assert manager.pause(args.name)['status'] == 'paused'
        try:
            client.screenshot()
        except RuntimeError as error:
            assert 'resume first' in str(error)
        else:
            raise AssertionError('paused environment accepted I/O')
        assert manager.resume(args.name)['status'] == 'running'
        assert control_is_held()
        client.action({'keyboard': {'keys_up': 'ctrl'}})
        assert not control_is_held()
        client.screenshot()
        assert client.last_metadata['generation'] != generation
        assert state()['nonce'] == before['nonce']
        report['pause_resume'] = True
        label = args.name + '-fastio-' + uuid.uuid4().hex[:8]
        mode = 'filesystem' if args.gpu else 'live'
        saved = manager.save(args.name, label, mode=mode, local_only=True)
        client.screenshot()  # Saving detached the old shared buffer.
        clone = args.name + '-clone-' + uuid.uuid4().hex[:6]
        try:
            manager.load(saved['snapshot'], clone)
            with manager.fast_io(clone) as restored:
                wait(lambda: restored.screenshot().size == (1280,800), timeout=60)
                restored.screenshot().save(output / 'restored.png')
                if not args.gpu:
                    assert state(clone)['nonce'] == before['nonce']
                    assert state(clone)['text'] == before['text']
                    restored.action({'mouse': {'left_click': [400,400]}})
                    wait(lambda: state(clone)['clicks'] > before['clicks'])
            report['save_load'] = {'snapshot': saved, 'app_ram_restored': not args.gpu}
        finally:
            manager.stop(clone, discard=True)
        assert not list((manager.local/'gvisor/fast-io'/clone).glob('*.frame'))
    report['passed'] = True
    (output/'report.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))
    client.close()


if __name__ == '__main__':
    main()
