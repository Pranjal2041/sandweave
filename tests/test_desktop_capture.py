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

    desktop.client = SimpleNamespace(screenshot=screenshot)
    return desktop, calls


def test_one_public_call_returns_png_and_matching_metadata():
    worker, calls = attached()
    def call(operation, **args):
        assert operation == 'control' and args['name'] == 'desktop'
        return worker.call(args['method'], args['parameters'])
    client = Desktop(SimpleNamespace(_call=call), {})
    result = client.capture_png()
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


def test_unsupported_helper_and_invalid_cursor_fail_explicitly():
    worker, _ = attached()
    with pytest.raises(ValueError, match='boolean'):
        worker.call('capture_png', {'cursor': 'yes'})
    worker.client.screenshot = lambda **_: Image.new('RGB', (1, 1))
    worker.client.last_metadata = {}
    with pytest.raises(UnsupportedFeature, match='rebuild'):
        worker.call('capture_png', {'cursor': True})
