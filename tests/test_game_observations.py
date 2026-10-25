import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import threading
import types

import pytest

from sandweave.sandbox.errors import UnsupportedFeature
from sandweave.sandbox.runtimes.modal import configure
from sandweave.sandbox.runtimes.modal import driver as modal_driver
from sandweave.steps import run
from sandweave.templates.controls import BUILTINS, provider
from sandweave.templates.resolve import Template

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_builtin_control_provider_loads():
    for name in BUILTINS:
        value = provider(name)
        assert value.descriptor({})['version'] >= 1 and callable(value.bind) and callable(value.attach)


def test_modal_vr_templates_capture_audio_before_the_game_starts():
    recipe = configure(Template('vr/opensaber').resolve())
    assert list(recipe['capabilities'])[:2] == ['audio', 'vr']
    assert recipe['capabilities']['audio'] == {'provider': 'audio', 'user': 'ga'}


def test_vr_haptics_records_match_the_monado_layout(tmp_path):
    guest = load(Path(modal_driver.__file__).with_name('guest_vr.py'), 'guest_vr_test')
    assert guest.HAPTIC.size == 40  # _Static_assert in notes/monado-vr-lab.patch
    path = tmp_path / 'haptics.bin'
    # An OpenXR stop, and (as Open Saber sends each frame) zero amplitude with the remaining duration.
    records = [guest.HAPTIC.pack(b'HAP1', 1, 100, 50_000_000, 160.0, .5, 0x30, 0),
               guest.HAPTIC.pack(b'HAP1', 0, 200, 0, 0.0, 0.0, 0x30, 0),
               guest.HAPTIC.pack(b'HAP1', 1, 300, 88_888_888, 20.0, 0.0, 0x30, 0)]
    path.write_bytes(b''.join(records) + records[0][:12])
    events = guest.read_haptics(path, 0)
    assert events[:2] == [{'device': 'right', 'guest_ns': 100, 'amplitude': .5, 'frequency_hz': 160.0,
                           'duration_ns': 50_000_000, 'stop': False},
                          {'device': 'left', 'guest_ns': 200, 'amplitude': 0.0, 'frequency_hz': 0.0,
                           'duration_ns': 0, 'stop': True}]
    assert events[2]['stop'] and events[2]['duration_ns'] == 88_888_888
    assert guest.read_haptics(path, 1) == events[1:] and guest.read_haptics(path, 3) == []
    guest.MAX_HAPTIC_EVENTS = 2  # one read stays bounded; the cursor continues from 'next'
    assert len(guest.read_haptics(path, 0)) == 2 and len(guest.read_haptics(path, 2)) == 1
    path.write_bytes(b'XXXX' + records[0][4:])
    with pytest.raises(ValueError):
        guest.read_haptics(path, 0)
    patch = (ROOT / 'notes/monado-vr-lab.patch').read_text()
    assert 'XRT_LAB_HAPTICS_FILE' in patch and 'sizeof(struct r_lab_haptic_record) == 40' in patch
    assert 'if (silent && rd->lab_silent)' in patch


def test_audio_ring_tiles_reads_and_reports_dropped_frames():
    audio = load(Path(modal_driver.__file__).with_name('guest_audio.py'), 'guest_audio_test')
    capture = object.__new__(audio.Capture)
    capture.condition, capture.error = threading.Condition(), None
    frame = audio.FRAME_BYTES
    capture.buffer, capture.base, capture.frames = bytearray(b'\x01' * frame * 480), 0, 480
    capture.received_ns = 10_000_000_000
    first = capture.read(0)
    assert first['frames'] == 480 and first['next'] == 480 and len(first['pcm']) == 480 * frame
    assert first['end_ns'] - first['start_ns'] == 10_000_000 and first['dropped_frames'] == 0
    assert capture.read(480)['frames'] == 0
    capture.buffer, capture.base, capture.frames = bytearray(b'\x02' * frame * 100), 1000, 1100
    late = capture.read(480)
    assert late['first_frame'] == 1000 and late['dropped_frames'] == 520 and late['frames'] == 100
    with pytest.raises(ValueError):
        capture.read(2000)
    capture.error = 'audio capture stopped: parec exited'
    with pytest.raises(RuntimeError):
        capture.read(1100)


def test_steps_return_audio_and_haptics_with_game_time():
    calls = []
    vr = types.SimpleNamespace(action=lambda state: calls.append(state) or {'guest_ack_ns': 1},
                               haptics=lambda: {'events': [{'device': 'right'}], 'next': 1, 'guest_ns': 50})
    audio = types.SimpleNamespace(observe=lambda: {'pcm': b'', 'frames': 0, 'end_ns': 40})
    sandbox = types.SimpleNamespace(capabilities={'vr': {}, 'audio': {}}, vr=vr, audio=audio)
    result = run(sandbox, {'id': 's2', 'observe': ['audio', 'haptics'],
                           'actions': [{'kind': 'xr_pose', 'device': 'right', 'position': [0, 1, 0]}]})
    assert result['observations']['audio']['game_time_ns'] == 40
    haptics = result['observations']['haptics']
    assert haptics['game_time_ns'] == 50 and haptics['value']['vr']['events'] == [{'device': 'right'}]
    assert haptics['step_id'] == 's2' and haptics['last_action_index'] == 0
    with pytest.raises(UnsupportedFeature):
        run(types.SimpleNamespace(capabilities={'desktop': {}}), {'observe': ['haptics']})


@pytest.mark.skipif(sys.platform == 'win32', reason='the writer uses POSIX select on stdin')
def test_gamepad_writer_reports_rumble_and_keeps_state(tmp_path):
    writer = load(ROOT / 'scripts/gamepad-input.py', 'gamepad_input_test')
    haptics = tmp_path / 'haptics.bin'
    process = subprocess.Popen([sys.executable, '-u', str(ROOT / 'scripts/gamepad-input.py'), '--serve',
                                '--state', str(tmp_path / 'pad.bin'), '--haptics', str(haptics)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def request(value):
        process.stdin.write((json.dumps(value) + '\n').encode())
        process.stdin.flush()
        return json.loads(process.stdout.readline())

    try:
        assert json.loads(process.stdout.readline())['ready']
        assert request({'haptics_after': 0})['events'] == []
        haptics.write_bytes(writer.RUMBLE.pack(b'RUM1', 0, 7, 0xFFFF, 0, 250) +
                            writer.RUMBLE.pack(b'RUM1', 1, 8, 0, 0, 0))
        reply = request({'haptics_after': 0})
        assert reply['next'] == 2 and reply['events'][0] == {
            'output': 'motors', 'guest_ns': 7, 'low_frequency': 1.0, 'high_frequency': 0.0,
            'duration_ms': 250, 'stop': False}
        assert reply['events'][1]['output'] == 'triggers' and reply['events'][1]['stop']
        assert request({'buttons_down': ['a']})['buttons'] == ['a']
        assert request({'haptics_after': 2})['events'] == []
        assert 'error' in request({'haptics_after': -1})
        assert request({'axes': {'lx': .5}})['buttons'] == ['a']
    finally:
        process.stdin.close()
        process.wait(timeout=5)
    mask = struct.unpack_from('<8sQQ8hIB3x', (tmp_path / 'pad.bin').read_bytes())[11]
    assert mask == 0
