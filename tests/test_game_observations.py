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


# Records in each game's real telemetry format (from the frontier games' eval runs).
TELEMETRY = {
    'speed-dreams': ('journal.jsonl', [
        {'ev': 'session', 'mono': 24.7, 'sid': 1, 't': -2.0, 'mult': 1, 'state': 8, 'session': 'Qualifying',
         'type': 'qualifications', 'laps': 0, 'duration': 600.0, 'raceman': 'GT Corkscrew Challenge',
         'track': {'name': 'Corkscrew', 'length': 3608.4}, 'cars': [{'idx': 0, 'name': 'Player'},
                                                                    {'idx': 1, 'name': 'USR Vulture'}]},
        {'ev': 'f', 'mono': 152.6, 'sid': 1, 't': 1.0, 'mult': 1, 'state': 1,
         'cars': [[0, 0, 2, 3598.45, 446.68, 51.33, 0.0, 0, 0, 31.754], [1, 0, 1, 3591.45, 439.68, 47.33, 0.0, 0, 0, 29.1]]},
        {'ev': 'lap', 'mono': 261.7, 'sid': 1, 't': 107.1, 'mult': 1, 'state': 1, 'idx': 1, 'lap': 1, 'time': 103.988,
         'valid': 1, 'best': 103.988, 'pos': 1, 'splits': [38.0, 65.2]},
        {'ev': 'timemult', 'mono': 1643.7, 'sid': 2, 't': 613.4, 'mult': 4, 'state': 1}]),
    'open-saber': ('journal-02.jsonl', [
        {'type': 'map_start', 'attempt': 1, 'difficulty': 'Easy', 'bpm': 127, 'seq': 9, 'settings': {}},
        {'type': 'note_cut', 'attempt': 1, 'combo': 1, 'multiplier': 1, 'score': 115, 'song_t': 3.0},
        {'type': 'note_miss', 'attempt': 1, 'score': 115, 'song_t': 4.0},
        {'type': 'map_end', 'attempt': 1, 'score': 115, 'percent': .5, 'max_combo': 1, 'right_notes': 1,
         'wrong_notes': 1, 'full_combo': False}]),
    'openttd': ('game.log', [
        'dbg: [script:2] [18] [P] GACR {"seq":1,"tok":"6adb","date":"1950-01-01","k":"start","settings":{}}',
        'dbg: [script:2] [18] [P] GACR {"seq":3,"date":"1950-02-01","co":[{"id":0,"name":"Bartown","bank":21286,'
        '"veh":3}],"paused":false,"k":"m","tok":"6adb"}',
        'dbg: [script:2] [18] [P] GACR {"seq":4,"date":"1950-02-02","k":"pause","paused":true,"tok":"6adb"}']),
    'wesnoth': ('game.log', [
        '[ga-audit] plugin started', '[ga-audit] context time=1792865508 name=Game',
        '[ga-audit] event seq=11 time=1792870232 turn=11 result=VICTORY scenario=The Elves Besieged',
        '[ga-audit] event seq=12 time=1792870300 turn=1 result=NONE scenario=Blackwater Port']),
}


def test_state_exporters_read_each_games_telemetry_incrementally(tmp_path):
    exporter = ROOT / 'src/sandweave/games/state_export.py'
    states = {}
    for game, (name, records) in TELEMETRY.items():
        source = tmp_path / game / name
        source.parent.mkdir()
        data = ''.join((r if isinstance(r, str) else json.dumps(r)) + '\n' for r in records).encode()
        cut = len(data) // 2  # mid-line: the partial line waits for the next read
        reads = []
        for chunk in (data[:cut], data[cut:]):
            with open(source, 'ab') as output:
                output.write(chunk)
            argv = [sys.executable, str(exporter), game, '--cache', str(tmp_path / 'cache'),
                    '--source', str(source.parent if game == 'open-saber' else source)]
            reads.append(json.loads(subprocess.run(argv, capture_output=True, text=True, check=True).stdout))
        assert sum(read['new_bytes'] for read in reads) == len(data)
        states[game] = reads[-1]['state']
    racing = states['speed-dreams']
    assert racing['session']['track']['name'] == 'Corkscrew' and racing['sim']['mult'] == 4
    assert [car['name'] for car in racing['standings']] == ['USR Vulture', 'Player'] and racing['laps']['1']['best'] == 103.988
    saber = states['open-saber']
    assert saber['counts'] == {'note_cut': 1, 'note_miss': 1} and saber['score'] == 115 and not saber['active']
    assert saber['results'][0]['percent'] == .5 and saber['map']['difficulty'] == 'Easy'
    assert states['openttd']['companies'][0]['bank'] == 21286 and states['openttd']['paused'] is True
    wesnoth = states['wesnoth']
    assert wesnoth['scenario'] == 'Blackwater Port' and wesnoth['turn'] == 1 and wesnoth['context'] == 'Game'
    assert wesnoth['scenarios']['The Elves Besieged']['results'][0]['result'] == 'VICTORY'


def test_steps_observe_state_through_the_games_exporter():
    exporter = types.SimpleNamespace(observe=lambda sandbox: {'state': {'turn': 3}, 'guest_ns': 70})
    sandbox = types.SimpleNamespace(capabilities={})
    result = run(sandbox, {'id': 's3', 'observe': ['state']}, state=exporter)
    assert result['observations']['state']['game_time_ns'] == 70
    assert result['observations']['state']['value']['state'] == {'turn': 3}
    with pytest.raises(UnsupportedFeature):
        run(sandbox, {'observe': ['state']})
    from sandweave.games.state import GameState
    with pytest.raises(ValueError):
        GameState('open-saber')
    assert GameState('openttd').command()[-1] == 'openttd'


def test_game_time_control_drives_waits_and_speed():
    calls = []

    class Clock:
        time_state = None

        def time(self, scale=None, advance_ticks=None, timeout=10):
            calls.append((scale, advance_ticks))
            self.time_state = {'paused': scale == 0 or advance_ticks is not None, 'time_scale': scale or 1.0,
                               'physics_ticks_per_second': 90}
            return self.time_state

    sandbox = types.SimpleNamespace(capabilities={'vr': {'time_control': 'sandweave-time'}}, vr=Clock())
    run(sandbox, {'observe': [], 'actions': [{'kind': 'game_speed', 'scale': 0}, {'kind': 'wait', 'ticks': 45},
                                             {'kind': 'wait', 'ms': 500}]})
    # Paused: ticks advance exactly, and 500 ms of game time is 45 ticks at 90 per second.
    assert calls == [(0, None), (None, 45), (None, 45)]
    calls.clear()
    run(sandbox, {'observe': [], 'actions': [{'kind': 'game_speed', 'scale': 4}, {'kind': 'wait', 'ms': 40}]})
    assert calls == [(4, None)]  # running at 4x: 40 ms of game time is a 10 ms wall wait
    with pytest.raises(ValueError):
        run(sandbox, {'observe': [], 'actions': [{'kind': 'wait', 'ticks': 3}]})
    with pytest.raises(UnsupportedFeature):
        run(types.SimpleNamespace(capabilities={'vr': {}}), {'observe': [], 'actions': [{'kind': 'game_speed', 'scale': 0}]})
    from sandweave.actions import action_from_dict
    with pytest.raises(ValueError):
        action_from_dict({'kind': 'game_speed', 'scale': 5})


def test_open_saber_build_includes_journal_and_time_control():
    template = (ROOT / 'src/sandweave/templates/vr/opensaber/template.toml').read_text()
    assert 'time_control = "sandweave-time"' in template
    patch = (ROOT / 'notes/open-saber-time-control.patch').read_text()
    assert 'SandweaveTime="*res://game/scripts/SandweaveTime.gd"' in patch and 'advance_ticks' in patch
    warmup = (ROOT / 'notes/open-saber-warmup.patch').read_text()
    assert 'SandweaveWarmup="*res://game/scripts/SandweaveWarmup.gd"' in warmup and 'particles.fire()' in warmup
    install = (ROOT / 'scripts/install-sandbox-vr.sh').read_text()
    assert all(f'open-saber-{name}.patch' in install for name in ('journal', 'time-control', 'warmup', 'xr-probe'))
    probe = (ROOT / 'notes/open-saber-xr-probe.patch').read_text()
    assert 'SandweaveXRProbe="*res://game/scripts/SandweaveXRProbe.gd"' in probe and '"xr_pose"' in probe
    launch = (ROOT / 'scripts/vr-monado-guest.sh').read_text()
    assert '--main-pack' in launch and 'SANDWEAVE_TIME_CONTROL' in launch and 'OPEN_SABER_JOURNAL_KEY' in launch
    assert 'SANDWEAVE_XR_PROBE=$state/xr-probe.jsonl' in launch


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
        haptics.write_bytes(writer.RUMBLE.pack(b'RUM1', 0, 0, 7, 0xFFFF, 0, 250) +
                            writer.RUMBLE.pack(b'RUM1', 1, 1, 8, 0, 0, 0))
        reply = request({'haptics_after': 0})
        assert reply['next'] == 2 and reply['events'][0] == {
            'output': 'motors', 'pad': 0, 'guest_ns': 7, 'low_frequency': 1.0, 'high_frequency': 0.0,
            'duration_ms': 250, 'stop': False}
        assert reply['events'][1]['output'] == 'triggers' and reply['events'][1]['pad'] == 1 and reply['events'][1]['stop']
        assert request({'buttons_down': ['a']})['buttons'] == ['a']
        assert request({'haptics_after': 2})['events'] == []
        assert 'error' in request({'haptics_after': -1})
        assert request({'axes': {'lx': .5}})['buttons'] == ['a']
        assert request({'axes': {'lt': 1}})['axes']['lt'] == 1.0 and 'error' in request({'axes': {'rt': -.1}})
    finally:
        process.stdin.close()
        process.wait(timeout=5)
    record = struct.unpack_from('<8sQQ8hIB3x', (tmp_path / 'pad.bin').read_bytes())
    assert record[11] == 0 and list(record[3:11]) == [0, 0, -32768, 0, 0, -32768, 0, 0]  # neutral: triggers released
