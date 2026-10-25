"""Worker side of the Modal VR stream, with the same interface AttachedVR uses for VRStream.

Monado, its paired-eye ring and the input bridge run inside the sandbox under
guest_io.py. Frames are encoded in the guest (JPEG by default; codec='zstd'
for lossless raw) and travel over one persistent TLS connection, with
concurrent requests pipelined on it.
"""
import io
import threading
import time

from ...errors import OperationUnknown


class RemoteVRStream:
    def __init__(self, runtime, context, *, width=960, height=1080, fps=90, hz=90, mirror='none', slots=8,
                 codec='jpeg', quality=90):
        if hz not in (60, 72, 90, 120, 144) or mirror not in ('none', 'pbo', 'sync'):
            raise ValueError('invalid compositor rate or mirror mode')
        if codec not in ('jpeg', 'zstd') or type(quality) is not int or not 1 <= quality <= 95:
            raise ValueError('VR transport codec must be jpeg (quality 1..95) or zstd')
        self.runtime, self.context = runtime, context
        self.options = dict(width=width, height=height, fps=fps, hz=hz, slots=slots)
        self.codec, self.quality = codec, quality
        self.connection = None
        self.sequence, self.input_mutex = 0, threading.Lock()
        self.broken = self.closed = False
        self.clock_rtt_ns = self.guest_to_host_ns = None

    def _call(self, op, **parameters):
        return self.connection.call(op, **parameters)

    def __enter__(self):
        deadline = time.monotonic() + self.context.remaining(60)
        while True:
            try:
                self.connection = self.runtime.io_connection(self.context.id, timeout=30)
                break
            except (OSError, ConnectionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError('guest I/O relay did not become reachable')
                time.sleep(.5)
        self._call('vr_start', **self.options)
        self._synchronize(20)
        return self

    def _synchronize(self, count):
        # Clock domains differ; bound their offset with the lowest round trip.
        samples = []
        for _ in range(count):
            sent = time.monotonic_ns()
            reply = self._call('ping')
            received = time.monotonic_ns()
            samples.append((received - sent, (sent + received) // 2 - reply['guest_ns']))
        self.clock_rtt_ns, self.guest_to_host_ns = min(samples)

    def input(self, state):
        with self.input_mutex:
            if self.broken:
                raise RuntimeError('input channel failed; actions cannot be replayed safely')
            self.sequence += 1
            sent = time.monotonic_ns()
            try:
                reply = self._call('vr_input', request={'op': 'input', 'id': self.sequence, 'state': state})
            except (OSError, OperationUnknown, TimeoutError, ConnectionError):
                self.broken = True
                raise
            received = time.monotonic_ns()
            if 'error' in reply:
                raise ValueError(reply['error'])
            if reply.get('id') != self.sequence:
                self.broken = True
                raise ConnectionError('input acknowledgement ID mismatch')
            return {**reply, 'host_sent_ns': sent, 'host_ack_ns': received, 'round_trip_ns': received - sent}

    def latest_encoded(self, after=0, timeout=5):
        """Frame metadata and its guest-encoded bytes, for forwarding without re-encoding."""
        value = self._call('vr_latest', after=after, timeout=timeout, format=self.codec, quality=self.quality)
        return value, value.pop('image'), value.pop('format')

    def latest(self, after=0, timeout=5):
        from vr_frames import Frame
        value, data, image_format = self.latest_encoded(after, timeout)
        expected = value['width'] * value['height'] * 4
        if image_format == 'zstd':
            import zstandard
            rgba = zstandard.ZstdDecompressor().decompress(data, max_output_size=expected)
        else:
            from PIL import Image
            rgba = Image.open(io.BytesIO(data)).convert('RGBX').tobytes()
        if len(rgba) != expected:
            raise ValueError('invalid decoded VR frame length')
        return Frame(rgba=rgba, **value)

    def suspend_attachment(self):
        pass

    def resume_attachment(self):
        self._synchronize(5)

    def abort_attachment(self):
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.connection is not None:
                self._call('vr_close')
        except Exception:
            pass
        finally:
            if self.connection is not None:
                self.connection.close()

    def __exit__(self, *args):
        self.close()
