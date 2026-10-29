import json
import time
from pathlib import Path
import sys
import types

import pytest

from sandweave.sandbox.admission import reservation
from sandweave.sandbox.errors import UnsupportedFeature
from sandweave.sandbox.resources import normalize
from sandweave.sandbox.runtimes.gvisor.driver import agent_source
from sandweave.sandbox.runtimes.modal import configure
from sandweave.sandbox.runtimes.modal import driver as modal_driver
from sandweave.templates.resolve import Template


def spec_for(template, **resources):
    from sandweave import Memory
    recipe = configure(Template(template).resolve())
    defaults = recipe['resources']
    return {'template': recipe, 'runtime': 'modal', 'mounts': [], 'ttl': None, 'startup_timeout': 30,
            'resources': normalize(cpu=resources.get('cpu', defaults['cpu']),
                                   memory=Memory(defaults['memory'], defaults['runtime_memory']),
                                   gpu=resources.get('gpu', defaults.get('gpu', False)),
                                   network=resources.get('network', 'internet'))}


def test_vr_templates_run_without_their_systemd_desktop():
    recipe = configure(Template('vr/opensaber').resolve())
    assert 'vr' in recipe['capabilities'] and 'desktop' not in recipe['capabilities']
    assert 'init' not in recipe['runtime_options'] and 'init_command' not in recipe['runtime_options']
    assert recipe['env']['DISPLAY'] == ':1'


@pytest.mark.parametrize('template', ['gnome', 'coding'])
def test_other_templates_are_rejected(template):
    with pytest.raises(UnsupportedFeature):
        configure(Template(template).resolve())


def test_resources_map_to_modal_reservations(monkeypatch):
    for name in ('SANDWEAVE_MODAL_GPU', 'SANDWEAVE_MODAL_REGION', 'SANDWEAVE_MODAL_CLOUD', 'MODAL_REGION',
                 'MODAL_CLOUD_PROVIDER'):
        monkeypatch.delenv(name, raising=False)
    options = modal_driver.Runtime.resources(spec_for('vr/opensaber'))
    assert options['gpu'] == 'L40S' and options['cpu'] == 6.0 and not options['block_network']
    assert options['memory'] == (16 + 2) * 1024
    from sandweave import GPU
    options = modal_driver.Runtime.resources(spec_for('vr/opensaber', gpu=GPU(model='A10'), network='offline'))
    assert options['gpu'] == 'A10' and options['block_network']
    assert 'region' not in options and 'cloud' not in options
    # A worker on Modal places sandboxes beside itself by default.
    monkeypatch.setenv('MODAL_REGION', 'us-east')
    monkeypatch.setenv('MODAL_CLOUD_PROVIDER', 'CLOUD_PROVIDER_OCI')
    options = modal_driver.Runtime.resources(spec_for('vr/opensaber'))
    assert options['region'] == ['us-east'] and options['cloud'] == 'oci'
    monkeypatch.setenv('SANDWEAVE_MODAL_REGION', 'us-east,us-central')
    monkeypatch.setenv('SANDWEAVE_MODAL_CLOUD', 'any')
    options = modal_driver.Runtime.resources(spec_for('vr/opensaber'))
    assert options['region'] == ['us-east', 'us-central'] and 'cloud' not in options
    with pytest.raises(UnsupportedFeature):
        modal_driver.Runtime.resources(spec_for('vr/opensaber', gpu=GPU(model=('A10', 'L4'))))
    spec = spec_for('vr/opensaber')
    spec['mounts'] = [{'source': '/tmp', 'destination': '/data', 'read_only': True}]
    with pytest.raises(UnsupportedFeature):
        modal_driver.Runtime.resources(spec)


def test_modal_sandboxes_do_not_reserve_worker_memory():
    assert reservation(spec_for('vr/opensaber')) == 0


def test_agent_can_read_its_token_from_a_file():
    source = agent_source(token_file=True)
    compile(source, 'agent', 'exec')
    assert source.rstrip().endswith('main(sys.argv[1], open(sys.argv[2]).read().strip(), image=False)')
    assert agent_source().rstrip().endswith('main(sys.argv[1], sys.argv[2], image=False)')


class FakeProcess:
    def __init__(self, output=b'', code=0):
        self.stdout, self.stderr, self.code = types.SimpleNamespace(read=lambda: output), \
            types.SimpleNamespace(read=lambda: ''), code

    def wait(self):
        return self.code


class FakeImage:
    def __init__(self):
        self.builds = 0

    def build(self, app):
        self.builds += 1


class FakeSandbox:
    created = []

    def __init__(self, *args, **options):
        self.args, self.options, self.object_id = args, options, 'sb-test'
        self.files, self.commands, self.terminated = {}, [], False
        self.filesystem = types.SimpleNamespace(write_bytes=lambda data, path: self.files.__setitem__(path, data))
        FakeSandbox.created.append(self)

    @classmethod
    def create(cls, *args, **options):
        return cls(*args, **options)

    @classmethod
    def from_id(cls, identity):
        return cls.created[-1]

    @classmethod
    def from_name(cls, app, name):
        return next(s for s in cls.created if s.options.get('name') == name)

    def poll(self):
        return 0 if self.terminated else None

    def exec(self, *argv, timeout=None):
        self.commands.append(argv)
        if argv[:2] == ('cat', '/run/sandweave/boot.json'):
            return FakeProcess(json.dumps({'ready': True}).encode())
        return FakeProcess()

    def tunnels(self, timeout=50):
        return {port: types.SimpleNamespace(host=f'{port}.modal.test', port=443) for port in (23799, 23801)}

    def terminate(self):
        self.terminated = True


def test_create_boots_the_guest_and_connects_to_its_agent(monkeypatch, tmp_path):
    (tmp_path / 'sandboxes').mkdir()
    fake = types.ModuleType('modal')
    fake.App = types.SimpleNamespace(lookup=lambda name, create_if_missing: types.SimpleNamespace(name=name))
    fake.Sandbox = FakeSandbox
    monkeypatch.setitem(sys.modules, 'modal', fake)
    import sandweave.sandbox.runtimes.modal.image as image
    built = FakeImage()
    monkeypatch.setattr(image, 'prepare', lambda recipe, root, app: built)
    calls = []

    class FakeConnection:
        def __init__(self, host, port, token, **options):
            calls.append((host, port, token, options))

        def call(self, op, **parameters):
            return {'pid': 1}

        def close(self):
            pass

    monkeypatch.setattr(modal_driver, 'Connection', FakeConnection)
    runtime = modal_driver.Runtime(tmp_path)
    spec = spec_for('vr/opensaber')
    metadata = runtime.create('vr-sw-1', spec)
    sandbox = FakeSandbox.created[-1]
    assert sandbox.args == ('python3', '-u', '/opt/sandweave/guest_boot.py')
    assert sandbox.options['encrypted_ports'] == [23799, 23801] and sandbox.options['gpu'] == 'L40S'
    assert sandbox.options['timeout'] == 24 * 3600 and sandbox.options['image'] is built and built.builds == 1
    assert sandbox.options['name'] == 'sandweave-vr-sw-1'
    assert sandbox.files['/run/sandweave/agent.token'].decode().strip() == metadata['token']
    assert b'open(sys.argv[2])' in sandbox.files['/run/sandweave/agent.py']
    assert metadata['host'] == '23799.modal.test' and metadata['io'] == {'host': '23801.modal.test', 'port': 443}
    assert all(options['tls'] for *_, options in calls)
    assert runtime.status('vr-sw-1')['status'] == 'running'
    runtime.terminate('vr-sw-1')
    assert sandbox.terminated and runtime.status('vr-sw-1')['status'] == 'stopped'
    runtime.discard('vr-sw-1')
    assert runtime.status('vr-sw-1')['status'] == 'missing'
    with pytest.raises(UnsupportedFeature):
        runtime.pause('vr-sw-1')


@pytest.mark.skipif(sys.platform != 'linux', reason='the paired-eye ring uses Linux memfd')
def test_guest_ring_matches_the_engine_ring_layout():
    import importlib.util
    from pathlib import Path
    import struct
    path = Path(modal_driver.__file__).with_name('guest_vr.py')
    sys.modules.setdefault('sandweave_wire', __import__('sandweave.sandbox.wire', fromlist=['wire']))
    spec = importlib.util.spec_from_file_location('guest_vr', path)
    guest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guest)
    ring = guest.Ring(32, 16, 2, 90)
    assert guest.CONFIG.unpack_from(ring.map, 0) == (b'VRRING2\0', 2, 64, 16, 2, 64 + 64 * 16 * 4, 256)
    offset = guest.HEADER_BYTES
    guest.SLOT_HEADER.pack_into(ring.map, offset, 2, 1, 7, 8, 9, 10, 11, 2)
    ring.map[offset + 64:offset + 64 + 4] = b'\x01\x02\x03\x04'
    struct.pack_into('<Q', ring.map, 32, 1)
    frame, pixels = ring.latest(0, 1)
    assert frame['sequence'] == 1 and frame['compositor_frame'] == 7 and frame['published_ns'] == 11
    assert frame['width'] == 64 and frame['eye_count'] == 2 and pixels[:4] == b'\x01\x02\x03\x04'


def test_session_stream_relays_frames_input_and_haptics_to_a_session_agent_socket(tmp_path):
    """A task's own VR session supervisor (vr_session.py protocol): JSON line, then payload bytes."""
    import socket
    import struct
    import threading
    sys.path.insert(0, str(Path(modal_driver.__file__).parent))
    import guest_vr
    import tempfile
    path, requests = Path(tempfile.mkdtemp(prefix='sw', dir='/tmp')) / 'a.sock', []  # AF_UNIX path limit
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen()

    def serve(connection):
        reader = connection.makefile('rb')
        for line in reader:
            request = json.loads(line)
            requests.append(request)
            if request['op'] == 'info':
                reply, payload = {'frame': {'width': 1920}}, b''
            elif request['op'] == 'observe':
                payload = b'\xff\xd8jpeg'
                reply = {'sequence': 7, 'compositor_frame': 3, 'predicted_display_ns': 1, 'capture_begin_ns': 50,
                         'gpu_ready_ns': 2, 'published_ns': 60, 'width': 1920, 'height': 1080, 'observed_ns': 70,
                         'bytes': len(payload)}
            elif request['state'] == 'bad':
                reply, payload = {'error': 'ValueError: bad state'}, b''
            else:
                reply, payload = {'ack': {'guest_received_ns': 10, 'guest_ack_ns': 11, 'input_sequence': 1}}, b''
            connection.sendall(json.dumps(reply).encode() + b'\n' + payload)

    def accept():
        while True:
            connection, _ = server.accept()
            threading.Thread(target=serve, args=(connection,), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()
    haptics = tmp_path / 'haptics.bin'
    haptics.write_bytes(struct.pack('<4sIQqffII', b'HAP1', 1, 5, 100, 160.0, .5, 0, 0))
    stream = guest_vr.SessionStream(types.SimpleNamespace(session_agent=str(path), session_haptics=str(haptics),
                                                          session_timeout=5))
    frame, payload = stream.latest_encoded(0, 5, 'jpeg', 90)
    assert payload == b'\xff\xd8jpeg' and frame['sequence'] == 7 and frame['width'] == 1920 and frame['eye_count'] == 2
    assert stream.request({'op': 'input', 'id': 4, 'state': {'right': {'trigger_value': 1.0}}}) == {
        'id': 4, 'guest_received_ns': 10, 'guest_ack_ns': 11, 'input_sequence': 1}
    assert requests[-1] == {'op': 'act', 'state': {'right': {'trigger_value': 1.0}}}
    assert 'error' in stream.request({'op': 'input', 'id': 5, 'state': 'bad'})
    assert stream.haptics(0)['events'][0]['device'] == 'right'
    with pytest.raises(RuntimeError):
        stream.game_time(scale=0)
    with pytest.raises(ValueError):
        stream.latest_encoded(0, 5, 'zstd', 90)
    stream.close()


def test_vr_session_template_keeps_its_audio_source():
    recipe = configure(Template('vr/opensaber-session').resolve())
    assert recipe['capabilities']['audio']['sink'] == 'vr'
    assert recipe['capabilities']['vr']['session_agent'].endswith('/current/agent.sock')


def test_placement_pins_only_what_the_worker_reports(monkeypatch):
    monkeypatch.delenv('SANDWEAVE_MODAL_REGION', raising=False)
    monkeypatch.delenv('SANDWEAVE_MODAL_CLOUD', raising=False)
    monkeypatch.setenv('MODAL_REGION', 'us-central')
    monkeypatch.setenv('MODAL_CLOUD_PROVIDER', 'CLOUD_PROVIDER_UNSPECIFIED')
    assert modal_driver.placement() == {'region': ['us-central']}
    monkeypatch.setenv('MODAL_CLOUD_PROVIDER', 'CLOUD_PROVIDER_OCI')
    assert modal_driver.placement() == {'region': ['us-central'], 'cloud': 'oci'}


def test_a_sandbox_modal_cannot_schedule_times_out_and_is_terminated(monkeypatch, tmp_path):
    import threading
    (tmp_path / 'sandboxes').mkdir()
    release = threading.Event()

    class Pending(FakeSandbox):
        @classmethod
        def create(cls, *args, **options):
            sandbox = cls(*args, **options)  # exists on Modal, but is not scheduled
            release.wait(5)
            return sandbox

        def terminate(self):
            self.terminated = True
            release.set()

    fake = types.ModuleType('modal')
    fake.App = types.SimpleNamespace(lookup=lambda name, create_if_missing: types.SimpleNamespace(name=name))
    fake.Sandbox = Pending
    monkeypatch.setitem(sys.modules, 'modal', fake)
    import sandweave.sandbox.runtimes.modal.image as image
    monkeypatch.setattr(image, 'prepare', lambda recipe, root, app: FakeImage())
    monkeypatch.setattr(modal_driver, 'SCHEDULE_FLOOR_S', 0.2)
    spec = spec_for('vr/opensaber')
    spec['startup_timeout'] = 0.2
    with pytest.raises(TimeoutError, match='did not schedule'):
        modal_driver.Runtime(tmp_path).create('vr-sw-2', spec)
    pending = FakeSandbox.created[-1]
    assert pending.options['name'] == 'sandweave-vr-sw-2' and pending.terminated


def test_a_sandbox_scheduled_after_the_deadline_is_terminated_when_modal_returns_it(monkeypatch, tmp_path):
    import threading
    (tmp_path / 'sandboxes').mkdir()
    release, returned = threading.Event(), threading.Event()

    class Late(FakeSandbox):
        @classmethod
        def create(cls, *args, **options):
            release.wait(5)  # Modal schedules it only after the deadline
            sandbox = cls(*args, **options)
            returned.set()
            return sandbox

        @classmethod
        def from_name(cls, app, name):
            raise LookupError('Sandbox not found')  # a pending create is not yet findable by name

    fake = types.ModuleType('modal')
    fake.App = types.SimpleNamespace(lookup=lambda name, create_if_missing: types.SimpleNamespace(name=name))
    fake.Sandbox = Late
    monkeypatch.setitem(sys.modules, 'modal', fake)
    import sandweave.sandbox.runtimes.modal.image as image
    monkeypatch.setattr(image, 'prepare', lambda recipe, root, app: FakeImage())
    monkeypatch.setattr(modal_driver, 'SCHEDULE_FLOOR_S', 0.2)
    spec = spec_for('vr/opensaber')
    spec['startup_timeout'] = 0.2
    with pytest.raises(TimeoutError, match='did not schedule'):
        modal_driver.Runtime(tmp_path).create('vr-sw-3', spec)
    release.set()
    assert returned.wait(5)
    late = FakeSandbox.created[-1]
    for _ in range(50):
        if late.terminated:
            break
        time.sleep(.05)
    assert late.options['name'] == 'sandweave-vr-sw-3' and late.terminated
