"""Encoded desktop observations, measured through a throttled client link."""
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import statistics
import threading
import time

import pytest
from PIL import Image

from sandweave import Memory, Sandbox, SandboxError, UnsupportedFeature
from sandweave.sandbox.targets import Endpoint, local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit isolated worker required')]
MEMORY = Memory('4GiB', '1GiB')
# Desktop files prepared by 0.2.46 or newer provide cursor capture; older
# files must refuse it explicitly. Set this to require the updated helper.
REQUIRE_CURSOR = os.environ.get('SANDWEAVE_DESKTOP_CURSOR_CAPTURE') == '1'
MBIT = 20
EVIDENCE = {}


def record(name, value):
    EVIDENCE[name] = value
    path = os.environ.get('SANDWEAVE_CAPTURE_EVIDENCE')
    if path:
        Path(path).write_text(json.dumps(EVIDENCE, indent=2, sort_keys=True) + '\n')


class ThrottledRelay:
    """A loopback TCP relay that limits worker-to-client bytes like a slow link."""
    def __init__(self, port, megabits):
        self.target, self.rate = port, megabits * 125_000
        self.server = socket.create_server(('127.0.0.1', 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            upstream = socket.create_connection(('127.0.0.1', self.target))
            threading.Thread(target=self._pump, args=(client, upstream, None), daemon=True).start()
            threading.Thread(target=self._pump, args=(upstream, client, self.rate), daemon=True).start()

    @staticmethod
    def _pump(source, destination, rate):
        try:
            while data := source.recv(65536):
                destination.sendall(data)
                if rate:
                    time.sleep(len(data) / rate)
        except OSError:
            pass
        finally:
            for item in (source, destination):
                try:
                    item.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self):
        self.server.close()


@pytest.fixture(scope='module', autouse=True)
def stop_idle_worker():
    yield
    connection = local_connection()
    deadline = time.monotonic() + 15
    try:
        while True:
            try:
                connection.call('_shutdown_if_idle')
                break
            except SandboxError as error:
                if str(error) != 'worker still owns live sandboxes' or time.monotonic() > deadline:
                    raise
                time.sleep(.1)
    finally:
        connection.close()


def timed(function, count=5):
    durations, result = [], None
    for _ in range(count):
        started = time.perf_counter()
        result = function()
        durations.append(time.perf_counter() - started)
    return result, statistics.median(durations)


def test_encoded_observations_on_a_slow_link():
    with Sandbox(template='gnome', memory=MEMORY) as env:
        raw = env.desktop.screenshot()
        jpeg = env.desktop.capture_image(format='jpeg', quality=85)
        png = env.desktop.capture_image(format='png')
        assert jpeg.mime_type == 'image/jpeg' and png.mime_type == 'image/png'
        assert jpeg.size == png.size == raw.size == (1920, 1080)
        decoded = Image.open(BytesIO(jpeg.data))
        assert decoded.format == 'JPEG' and decoded.size == raw.size
        lossless = Image.open(BytesIO(png.data)).convert('RGB')
        assert lossless.size == raw.size
        assert jpeg.metadata['image_format'] == 'jpeg' and jpeg.metadata['image_quality'] == 85
        assert len(jpeg.data) * 10 < len(raw.tobytes())

        # One request applies the action and returns the encoded next frame.
        observation = env.desktop.step({'mouse': {'move': [960, 540]}}, format='jpeg', quality=80)
        assert observation.mime_type == 'image/jpeg' and observation.image.size == (1920, 1080)
        assert observation.metadata['image_quality'] == 80
        assert observation.metadata['input_sequence'] >= 1
        assert env.desktop.step({'mouse': {'move': [10, 10]}}).data is None

        try:
            frame = env.desktop.capture_image(format='jpeg', cursor=True)
            cursor = True
        except UnsupportedFeature as error:
            assert not REQUIRE_CURSOR, error
            assert '0.2.46' in str(error)
            cursor = False
        if cursor:
            assert frame.metadata['cursor'] is True
            assert 0 < frame.metadata['capture_started_wall_ns'] <= frame.metadata['capture_completed_wall_ns']
            env.desktop.step({'mouse': {'move': [960, 540]}})
            plain = env.desktop.capture_png(cursor=False)
            pointer = env.desktop.capture_png(cursor=True)
            assert plain.metadata['cursor'] is False and pointer.metadata['cursor'] is True
            # Only the composited pointer distinguishes the two captures there.
            region = (944, 524, 992, 572)
            assert (Image.open(BytesIO(plain.png)).crop(region).tobytes() !=
                    Image.open(BytesIO(pointer.png)).crop(region).tobytes())
            screenshots = os.environ.get('SANDWEAVE_CAPTURE_SCREENSHOTS')
            if screenshots:
                Path(screenshots).mkdir(parents=True, exist_ok=True)
                Path(screenshots, 'cursor.png').write_bytes(env.desktop.capture_png(cursor=True).png)
                Path(screenshots, 'jpeg-q85.jpg').write_bytes(jpeg.data)
        else:
            assert 'cursor' not in jpeg.metadata or jpeg.metadata['cursor'] is False

        # Measure the same sandbox through a 20 Mbit/s worker-to-client link.
        connection = local_connection()
        port, token = connection.port, connection.token
        connection.close()
        relay = ThrottledRelay(port, MBIT)
        slow = Sandbox.connect(env.id, target=Endpoint(relay.port, token))
        try:
            _, raw_seconds = timed(lambda: slow.desktop.screenshot(), 3)
            _, jpeg_seconds = timed(lambda: slow.desktop.capture_image(format='jpeg', quality=85))
            _, step_seconds = timed(lambda: slow.desktop.step({'mouse': {'move': [960, 540]}}, format='jpeg'))
        finally:
            slow.close()
            relay.close()
        assert jpeg_seconds * 5 < raw_seconds
        record('capture', {'raw_rgb_bytes': len(raw.tobytes()), 'jpeg_q85_bytes': len(jpeg.data),
                           'png_bytes': len(png.data), 'jpeg_encode_ns': jpeg.metadata['image_encode_ns'],
                           'png_encode_ns': png.metadata['image_encode_ns'], 'link_mbit': MBIT,
                           'raw_screenshot_seconds': raw_seconds, 'jpeg_capture_seconds': jpeg_seconds,
                           'jpeg_step_seconds': step_seconds, 'cursor_capture': cursor})
