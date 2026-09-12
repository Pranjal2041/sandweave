"""Recording opt-in, bounded reads, authentication and failure recovery."""
import json
import time
from types import SimpleNamespace

import pytest

from sandweave import Recording, UnsupportedFeature
from sandweave.cli import parser, creation
from sandweave.sandbox.recording import normalize
from sandweave.sandbox.sandbox import definition
from sandweave.templates.gnome.recording import Recordings, run


@pytest.mark.parametrize('value', [0, 61, True, 1.5, '15'])
def test_invalid_cadence(value):
    with pytest.raises(ValueError, match='fps'):
        Recording(fps=value)


def test_opt_in_and_cli_contract():
    assert normalize(False) is None
    assert normalize(True) == {'fps': 15, 'cursor': True}
    assert 'recording' not in definition(template='gnome')['spec']
    assert definition(template='gnome', recording=Recording(fps=5, cursor=False))['spec']['recording'] == {
        'fps': 5, 'cursor': False}
    assert creation(parser().parse_args(['create', '--template', 'gnome', '--record']))['recording'] is True
    with pytest.raises(UnsupportedFeature, match='Xvnc'):
        definition(recording=True)
    with pytest.raises(UnsupportedFeature, match='gvisor'):
        definition(template='gnome', recording=True, runtime='apptainer')
    with pytest.raises(ValueError, match='cursor'):
        Recording(cursor='yes')


def worker(tmp_path):
    def validate(identity):
        assert identity == 'sw-test'
    return SimpleNamespace(root=tmp_path, path=validate)


def test_disabled_recording_does_not_create_files(tmp_path):
    recordings = Recordings(worker(tmp_path))
    assert recordings.status('sw-test')['state'] == 'off'
    assert recordings.stop('sw-test')['state'] == 'off'
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(FileNotFoundError, match='no recording'):
        recordings.dispatch('sw-test', 'manifest')


@pytest.mark.parametrize('parameters', [
    {'path': '../private'}, {'path': 'a'*32+'/control.json'},
    {'path': 'a'*32+'/video.mp4', 'size': 4*1024**2+1},
    {'path': 'a'*32+'/video.mp4', 'offset': -1},
])
def test_export_paths_and_chunks_are_bounded(tmp_path, parameters):
    with pytest.raises(ValueError):
        Recordings(worker(tmp_path)).dispatch('sw-test', 'read', **parameters)


def test_recording_scope_does_not_authorize_other_sandboxes(tmp_path):
    from sandweave.sandbox.management import Management
    manager = Management.__new__(Management)
    manager.read = lambda _: {'id': 'sw-one', 'token': 'secret'}
    assert manager.authorize('sw1.sw-one.secret', 'recording', {'identity': 'sw-one', 'action': 'read'})
    assert not manager.authorize('sw1.sw-one.secret', 'recording', {'identity': 'sw-two', 'action': 'read'})
    assert not manager.authorize('sw1.sw-one.wrong', 'recording', {'identity': 'sw-one', 'action': 'read'})


def test_export_reads_actual_mp4_filename(tmp_path):
    recordings = Recordings(worker(tmp_path))
    path = recordings.directory('sw-test')/('a'*32)/'video.mp4'
    path.parent.mkdir(parents=True)
    path.write_bytes(b'fragment')
    assert recordings.dispatch('sw-test', 'read', path='a'*32+'/video.mp4', offset=1, size=3) == b'rag'


def test_interrupted_capture_keeps_decodable_video_and_timeline(tmp_path):
    Image = pytest.importorskip('PIL.Image')
    imageio = pytest.importorskip('imageio_ffmpeg')
    class Source:
        def __init__(self, *args, **kwargs):
            self.frame_count = 0
        def frame(self):
            self.frame_count += 1
            if self.frame_count == 8:
                raise ConnectionError('display disappeared')
            started = time.monotonic_ns()
            time.sleep(.045)  # Intentionally slower than the requested 30 fps.
            return Image.new('RGB', (80, 60), (self.frame_count*30, 70, 10)), {
                'capture_started_ns': started, 'capture_completed_ns': time.monotonic_ns()}
        def close(self):
            pass
    segment = tmp_path/('a'*32)
    segment.mkdir()
    report = run(segment, {'port': 1, 'password': '', 'fps': 30, 'cursor': True,
                           'encoder': imageio.get_ffmpeg_exe()}, capture_factory=Source)
    assert report['state'] == 'partial' and 'display disappeared' in report['error']
    assert report['capture_slots_missed'] > 0
    assert report['frames_repeated'] > 0
    assert report['frames_encoded'] == report['frames_submitted']
    reader = imageio.read_frames(str(segment/'video.mp4'))
    try:
        next(reader)
        assert len(list(reader)) == report['frames_encoded']
    finally:
        reader.close()
    rows = [json.loads(line) for line in (segment/'timeline.jsonl').read_text().splitlines()]
    assert all(b['video_frame'] > a['video_frame'] for a, b in zip(rows, rows[1:]))


def test_dead_recorder_is_partial_and_files_can_be_exported(tmp_path):
    from sandweave.sandbox.workspace import atomic_json
    from sandweave.sandbox.ownership import process_scope
    recordings = Recordings(worker(tmp_path))
    root = recordings.directory('sw-test')
    segment = root/('b'*32)
    segment.mkdir(parents=True)
    atomic_json(root/'control.json', {'desired': 'recording', 'options': normalize(True)})
    atomic_json(segment/'segment.json', {'id': segment.name, 'state': 'recording', 'started_at': 1,
        'process': {'pid': 2147483647, 'started': '0', 'scope': process_scope()}})
    (segment/'video.mp4').write_bytes(b'completed fragments')
    assert recordings.status('sw-test')['state'] == 'partial'
    recordings.stop('sw-test')
    manifest = recordings.dispatch('sw-test', 'manifest')
    assert any(file['path'].endswith('/video.mp4') for file in manifest['files'])
    recordings.dispatch('sw-test', 'delete')
    assert not root.exists()
