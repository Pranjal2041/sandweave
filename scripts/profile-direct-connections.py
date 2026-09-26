#!/usr/bin/env python3
"""Compare public desktop APIs through an outbound relay and a direct connection.

Run inside an existing CPU allocation with prepared GNOME assets. The selected
directory must be a fresh, disposable workspace. No GPU or host sudo is used.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import socket
import time
import uuid

from sandweave import Cluster, Memory, Pool, Sandbox
from sandweave.sandbox.targets import local_connection
from sandweave.weave.worker import Bridge


def capacity(path, growth=24 * 1024**3):
    space = shutil.disk_usage(path)
    if space.free - growth < space.total * .15:
        raise RuntimeError('test would leave less than 15% free on its filesystem')


def summarize(values):
    values = sorted(values)
    return {'count': len(values), **{key: round(values[int((len(values) - 1) * p)] * 1000, 3)
            for key, p in [('p50_ms', .5), ('p95_ms', .95), ('max_ms', 1)]}}


def timed(function):
    start = time.perf_counter()
    function()
    return time.perf_counter() - start


def profile(root, samples):
    capacity(root)
    cluster = Cluster.start('direct-profile', directory=root / 'controller', local_worker=False)
    local, bridge, env = None, None, None
    handles = []
    report = {'hostname': socket.gethostname(), 'samples': samples,
              'conditions': 'same host, CPU-only GNOME, 1920x1080 RGB; outbound worker relay versus direct worker RPC'}
    try:
        local = local_connection()
        info = local.call('ping')
        channel = uuid.uuid4().hex
        bridge = Bridge({'url': cluster.info['connection']['address'], 'token': cluster.connection.token}, channel).start()
        cluster.add_worker({'endpoint': {'hostname': info['hostname'], 'port': info['port'],
            'token': local.token, 'relay': channel}}, slots=20, memory='24GiB')
        env = Sandbox(target=cluster, connection='direct', template='gnome', gpu=False,
                      memory=Memory('4GiB', '1GiB'), startup_timeout=600)
        application = '''import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk
from pathlib import Path
window = Gtk.Window(title='Sandweave direct connection acceptance')
window.set_default_size(760, 220)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
box.set_border_width(28)
box.pack_start(Gtk.Label(label='Keyboard input through a direct sandbox connection'), False, False, 0)
entry = Gtk.Entry()
entry.connect('changed', lambda widget: Path('/workspace/typed').write_text(widget.get_text()))
box.pack_start(entry, False, False, 0)
window.add(box)
window.connect('destroy', Gtk.main_quit)
window.show_all()
entry.grab_focus()
Gtk.main()
'''
        env.files.write_text('/workspace/direct-test.py', application)
        app = env.exec('python /workspace/direct-test.py')
        env.run("xdotool search --sync --name 'Sandweave direct connection acceptance' windowactivate --sync",
                timeout=30, check=True)
        env.run("xdotool search --name 'Sandweave direct connection acceptance' windowfocus --sync", check=True)
        env.desktop.keyboard.type('Direct input works: hello')
        deadline = time.monotonic() + 10
        while True:
            try:
                if env.files.read_text('/workspace/typed') == 'Direct input works: hello':
                    break
            except FileNotFoundError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError('keyboard input did not reach the application')
            time.sleep(.05)
        time.sleep(.5)
        env.desktop.screenshot().save(root / 'direct-input.png')
        for mode in ('cluster', 'direct'):
            handle = Sandbox.connect(env.id, target=cluster, connection=mode)
            handles.append(handle)
            counts = {}
            original = handle._connection.control.call
            def counted(op, **kw):
                counts[op] = counts.get(op, 0) + 1
                return original(op, **kw)
            handle._connection.control.call = counted
            def action():
                handle.desktop.action({'mouse': {'move': [700, 600]}})
            def step():
                result = handle.desktop.step({'mouse': {'move': [700, 600]}})
                assert result.image.size == (1920, 1080)
                assert result.metadata['frame_input_sequence'] >= result.metadata['input_sequence']
            for _ in range(5):
                action(); step()
            report[mode] = {'action': summarize([timed(action) for _ in range(samples)]),
                            'action_and_rgb_image': summarize([timed(step) for _ in range(samples)])}
            with ThreadPoolExecutor(max_workers=16) as threads:
                report[mode]['16_concurrent_actions'] = summarize(list(threads.map(
                    lambda _: timed(action), range(samples * 4))))
            report[mode]['controller_calls_during_measurement'] = counts
            if mode == 'direct':
                assert not counts, counts
            else:
                assert counts.get('sandbox_rpc', 0) > 0
            print(mode + ': ' + json.dumps(report[mode]), flush=True)
            capacity(root)
        # Exercise direct leases concurrently on real guests, including a
        # second checkout after every previous lease was returned.
        with Pool(target=cluster, connection='direct', size=8, warm=8,
                  memory=Memory('256MiB', '256MiB'), wait_timeout=600) as pool:
            def episode(index):
                with pool.acquire() as member:
                    start = time.perf_counter()
                    result = member.run('printf direct-' + str(index), check=True)
                    assert result.stdout == 'direct-' + str(index)
                    assert member._connection.connection_mode == 'direct'
                    return time.perf_counter() - start
            with ThreadPoolExecutor(max_workers=8) as threads:
                report['pool_commands'] = summarize(list(threads.map(episode, range(16))))
        report['input_verified'] = env.files.read_text('/workspace/typed') == 'Direct input works: hello'
        app.terminate()
        env.terminate()
        deadline = time.monotonic() + 60
        while any(not record['released'] for record in cluster.info['sandboxes']):
            if time.monotonic() > deadline:
                raise RuntimeError('controller did not release reservations')
            time.sleep(.1)
        report['reservations_released'] = True
        (root / 'latency.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2), flush=True)
    finally:
        for handle in handles:
            handle.close()
        if env:
            env.terminate()
            env.close()
        deadline = time.monotonic() + 60
        while any(not record['released'] for record in cluster.info['sandboxes']):
            if time.monotonic() > deadline:
                raise RuntimeError('test cleanup did not release reservations')
            time.sleep(.1)
        cluster.stop()
        cluster.close()
        if bridge:
            bridge.close()
        if local:
            local.call('_shutdown_if_idle')
            local.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--samples', type=int, default=50)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error('--samples must be positive')
    args.directory.mkdir(parents=True, exist_ok=True)
    if (args.directory / 'controller').exists():
        parser.error('choose a fresh directory')
    profile(args.directory.resolve(), args.samples)
