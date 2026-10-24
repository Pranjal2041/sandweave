import io
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from sandweave import UnsupportedFeature
from sandweave.templates.gnome.controls import AttachedDesktop, Desktop, encode_png


@pytest.mark.parametrize('size', [(1, 1), (7, 11), (1920, 1080)])
def test_png_preserves_every_rgb_pixel(size):
    image = Image.effect_noise(size, 80).convert('RGB')
    decoded = Image.open(io.BytesIO(encode_png(image)))
    assert decoded.size == size
    assert decoded.mode == 'RGB'
    assert decoded.tobytes() == image.tobytes()


def attached():
    desktop = AttachedDesktop.__new__(AttachedDesktop)
    desktop.capture_lock = threading.Lock()
    calls = []

    def screenshot(**options):
        calls.append(options)
        desktop.client.last_metadata = dict(frame_sequence=2, capture_completed_wall_ns=1234,
                                            cursor=options.get('cursor', False))
        return Image.new('RGB', (16, 9), (25, 30, 40))

    def step(action):
        calls.append({'action': action})
        return Image.new('RGB', (16, 9), (60, 70, 80)), dict(input_sequence=3, capture_completed_wall_ns=4321)

    desktop.client = SimpleNamespace(screenshot=screenshot, step=step)
    return desktop, calls


def test_one_public_call_returns_png_and_matching_metadata():
    worker, calls = attached()
    def call(operation, **args):
        assert operation == 'control' and args['name'] == 'desktop'
        return worker.call(args['method'], args['parameters'])
    client = Desktop(SimpleNamespace(_call=call), {})
    result = client.capture_png(cursor=True)
    assert calls == [{'cursor': True}]
    assert result.metadata['cursor'] is True
    assert result.metadata['frame_sequence'] == 2
    assert result.size == (16, 9)
    assert Image.open(io.BytesIO(result.png)).getpixel((0, 0)) == (25, 30, 40)
    assert client.screenshot().getpixel((0, 0)) == (25, 30, 40)
    assert calls[-1] == {'fresh': True}


def test_encoding_does_not_hold_capture_lock(monkeypatch):
    worker, _ = attached()
    def encode(image):
        assert worker.capture_lock.acquire(blocking=False)
        worker.capture_lock.release()
        return b'encoded'
    monkeypatch.setattr('sandweave.templates.gnome.controls.encode_png', encode)
    assert worker.call('capture_png', {'cursor': False})['png'] == b'encoded'


def test_older_helpers_capture_without_cursor_and_explain_cursor_requests():
    worker, _ = attached()
    with pytest.raises(ValueError, match='boolean'):
        worker.call('capture_png', {'cursor': 'yes'})
    def older(**options):
        if options.get('cursor'):
            raise RuntimeError('cursor capture requires rebuilt timestamped fast I/O assets')
        return Image.new('RGB', (1, 1))
    worker.client.screenshot = older
    worker.client.last_metadata = {'frame_sequence': 1}
    metadata = worker.call('capture_image', {})['metadata']
    assert metadata.pop('image_encode_ns') >= 0
    assert metadata == {'frame_sequence': 1, 'image_format': 'jpeg', 'image_quality': 85}
    with pytest.raises(UnsupportedFeature, match='0.2.46'):
        worker.call('capture_png', {'cursor': True})


@pytest.mark.parametrize('format', ['jpeg', 'png'])
def test_encoded_capture_preserves_size_cursor_and_metadata(format):
    worker, calls = attached()
    client = Desktop(SimpleNamespace(_call=lambda operation, **kw: worker.call(kw['method'], kw['parameters'])), {})
    result = client.capture_image(format=format, cursor=True)
    assert calls == [{'cursor': True}]
    assert result.mime_type == 'image/' + format
    assert result.metadata['image_format'] == format
    assert result.metadata['capture_completed_wall_ns'] == 1234
    image = Image.open(io.BytesIO(result.data))
    assert image.size == result.size == (16, 9)
    assert image.format.lower() == format


@pytest.mark.parametrize('options', [{'quality': 0}, {'quality': True}, {'quality': 96},
                                    {'format': 'gif'}, {'cursor': 'yes'}, {'resize': 720}])
def test_encoded_capture_rejects_bad_options_before_capture(options):
    worker, calls = attached()
    with pytest.raises(ValueError):
        worker.call('capture_image', options)
    assert not calls


def test_jpeg_encoding_releases_capture_lock(monkeypatch):
    worker, _ = attached()
    def encode(image, quality):
        assert worker.capture_lock.acquire(blocking=False)
        worker.capture_lock.release()
        assert quality == 85
        return b'jpeg'
    monkeypatch.setattr('sandweave.templates.gnome.controls.encode_jpeg', encode)
    assert worker.call('capture_image', {})['data'] == b'jpeg'


def test_capture_defaults_exclude_the_cursor():
    worker, calls = attached()
    client = Desktop(SimpleNamespace(_call=lambda operation, **kw: worker.call(kw['method'], kw['parameters'])), {})
    assert client.capture_image().mime_type == 'image/jpeg'
    client.capture_png()
    assert calls == [{'cursor': False}, {'cursor': False}]


@pytest.mark.parametrize('format', ['jpeg', 'png'])
def test_step_returns_an_encoded_observation_in_one_request(format):
    worker, calls = attached()
    requests = []
    def call(operation, **kw):
        requests.append(kw['method'])
        return worker.call(kw['method'], kw['parameters'])
    client = Desktop(SimpleNamespace(_call=call), {})
    observation = client.step({'mouse': {'move': [1, 2]}}, format=format, quality=70)
    assert requests == ['step'] and calls == [{'action': {'mouse': {'move': [1, 2]}}}]
    assert observation.mime_type == 'image/' + format
    assert Image.open(io.BytesIO(observation.data)).format.lower() == format
    assert observation.image.size == (16, 9) and observation.image.mode == 'RGB'
    assert observation.metadata['input_sequence'] == 3
    assert observation.metadata['image_quality'] == (70 if format == 'jpeg' else None)
    raw = client.step({'mouse': {'move': [1, 2]}})
    assert raw.data is None and raw.mime_type is None
    assert raw.image.getpixel((0, 0)) == (60, 70, 80)


@pytest.mark.parametrize('options', [{'format': 'gif'}, {'quality': 0}, {'resize': 2}])
def test_step_rejects_bad_options(options):
    worker, calls = attached()
    with pytest.raises(ValueError):
        worker.call('step', {'action': {}, **options})
    assert not calls


def test_jpeg_is_a_small_fraction_of_a_raw_desktop_frame():
    image = Image.effect_noise((1920, 1080), 20).convert('RGB').filter(__import__('PIL.ImageFilter', fromlist=['x']).GaussianBlur(4))
    from sandweave.templates.gnome.controls import encode_jpeg
    assert len(encode_jpeg(image, 85)) < len(image.tobytes()) / 20
