"""Authenticate, view and type through real GNOME streams on SDK transports."""
import json
import os
from pathlib import Path
import socket
import struct
import sys
import time

import pytest

from sandweave import Cluster, Memory, Sandbox
from sandweave.sandbox.targets import local_connection
from sandweave.weave.worker import Bridge

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_VNC_INTEGRATION'), reason='explicit disposable desktop worker required')]


def exact(stream, count):
    data = bytearray()
    while len(data) < count:
        part = stream.read(count - len(data))
        assert part, 'VNC disconnected'
        data.extend(part)
    return bytes(data)


def authenticate(stream, password):
    from Crypto.Cipher import DES
    assert exact(stream, 12) == b'RFB 003.008\n'
    stream.write(b'RFB 003.008\n')
    assert 2 in exact(stream, exact(stream, 1)[0])
    stream.write(b'\x02')
    key = bytes(int(f'{b:08b}'[::-1], 2) for b in password.encode()[:8].ljust(8, b'\0'))
    stream.write(DES.new(key, DES.MODE_ECB).encrypt(exact(stream, 16)))
    assert exact(stream, 4) == bytes(4)
    stream.write(b'\x01')
    size = struct.unpack('>HH', exact(stream, 4))
    exact(stream, 16)
    exact(stream, struct.unpack('>I', exact(stream, 4))[0])
    stream.write(bytes(4) + struct.pack('>BBBBHHHBBB3x', 32, 24, 0, 1, 255, 255, 255, 16, 8, 0))
    stream.write(struct.pack('>BBHi', 2, 0, 1, 0))  # Raw pixels, no pseudo-encodings.
    return size


def frame(stream, size):
    from PIL import Image
    stream.write(struct.pack('>BBHHHH', 3, 0, 0, 0, *size))
    image = Image.new('RGB', size)
    while True:
        kind = exact(stream, 1)[0]
        if kind == 2:
            continue
        if kind == 3:
            exact(stream, 3)
            exact(stream, struct.unpack('>I', exact(stream, 4))[0])
            continue
        assert kind == 0
        _, count = struct.unpack('>BH', exact(stream, 3))
        for _ in range(count):
            x, y, width, height, encoding = struct.unpack('>HHHHi', exact(stream, 12))
            assert encoding == 0
            image.paste(Image.frombytes('RGB', (width, height), exact(stream, width*height*4),
                                       'raw', 'BGRX'), (x, y))
        if count:
            return image


@pytest.fixture(scope='module')
def worker():
    from sandweave.templates.resolve import Template
    connection = local_connection(template=Template('gnome').resolve())
    try:
        yield connection
    finally:
        connection.call('_shutdown_if_idle')
        connection.close()


def exercise(source, target, name):
    artifact = Path(os.environ['SANDWEAVE_VNC_INTEGRATION'])
    artifact.mkdir(parents=True, exist_ok=True)
    env = Sandbox.connect(source.id, target=target)
    stream = None
    try:
        stream = env.desktop.vnc()
        size = authenticate(stream, env.info['vnc']['password'])
        assert size == (1920, 1080)
        env.files.write_text('/workspace/vnc-input.py', '''import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk
from pathlib import Path
window = Gtk.Window(title='Sandweave VNC stream acceptance')
window.set_default_size(800, 220)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
box.set_border_width(28)
box.pack_start(Gtk.Label(label='Keyboard input received through the SDK VNC stream'), False, False, 0)
entry = Gtk.Entry()
entry.connect('changed', lambda widget: Path('/workspace/vnc-typed').write_text(widget.get_text()))
box.pack_start(entry, False, False, 0)
window.add(box)
window.show_all()
entry.grab_focus()
Gtk.main()
''')
        process = env.exec('python /workspace/vnc-input.py')
        try:
            env.run("xdotool search --sync --name 'Sandweave VNC stream acceptance' windowactivate --sync windowfocus --sync",
                    timeout=15, check=True)
            expected = 'VNC stream: ' + name
            events = b''.join(struct.pack('>BBHI', 4, down, 0, ord(char))
                              for char in expected for down in (1, 0))
            stream.write(events)
            deadline = time.monotonic() + 5
            while env.run('cat /workspace/vnc-typed', check=False).stdout != expected:
                assert time.monotonic() < deadline
                time.sleep(.05)
            time.sleep(.3)
            started = time.monotonic()
            image = frame(stream, size)
            elapsed = time.monotonic() - started
            assert image.convert('L').getextrema()[1] > 100
            image.save(artifact / (name + '.png'))
            print(json.dumps({'transport': name, 'size': size, 'frame_seconds': elapsed,
                              'input': 'verified', 'id': env.id}))
        finally:
            process.terminate()
        env.pause()
        assert stream.read() == b''
        env.resume()
    finally:
        if stream:
            stream.close()
        env.close()


def test_local_and_ssh_desktop(worker):
    with Sandbox(template='gnome', cpu=2, memory=Memory('4GiB', '1GiB')) as env:
        exercise(env, None, 'local')
        if host := os.environ.get('SANDWEAVE_VNC_SSH'):
            # Metadata selects this owned worker; SSH uses the SDK's usual
            # endpoint tunnel, with no separate VNC forwarding arrangement.
            markers = list((Path(os.environ['SANDWEAVE_HOME']) / 'connections').glob('*/worker.json'))
            marker = next(path for path in markers if json.loads(path.read_text())['port'] == worker.port)
            exercise(env, {'host': host, 'metadata': str(marker), 'python': sys.executable}, 'ssh')


@pytest.mark.parametrize('relay', [False, True])
def test_weave_desktop(worker, relay):
    root = Path(os.environ['SANDWEAVE_VNC_INTEGRATION']) / ('relay' if relay else 'direct')
    cluster = Cluster.start('vnc-' + root.name, directory=root, local_worker=False)
    bridge = None
    remote = Cluster.connect(cluster.info['connection']['address'], token=cluster.connection.token)
    try:
        info = worker.call('ping')
        endpoint = {key: info[key] for key in ('hostname', 'port', 'workspace')}
        endpoint['token'] = worker.token
        if relay:
            endpoint['relay'] = 'e'*32
            bridge = Bridge(remote.config, endpoint['relay']).start()
        remote.add_worker({'endpoint': endpoint}, slots=2)
        with Sandbox(target=remote, template='gnome', cpu=2, memory=Memory('4GiB', '1GiB')) as env:
            exercise(env, remote, 'weave-' + root.name)
    finally:
        remote.stop()
        remote.close()
        cluster.close()
        if bridge:
            bridge.close()
            for thread in bridge.threads:
                thread.join(10)
