"""Experimental host API for continuous Monado observations and controller state.

A context owns the VR game/runtime, a bounded host-shared RGBA ring, and one
persistent acknowledged input channel. Close it before sandbox lifecycle changes.
"""
from dataclasses import dataclass
import ctypes
import fcntl
import io
import json
import mmap
import os
from pathlib import Path
import platform
import queue
import re
import select
import struct
import subprocess
import tarfile
import threading
import tempfile
from concurrent.futures import ThreadPoolExecutor
import time
import uuid

from environment import EnvironmentManager
from fast_io import io_lock

HEADER_BYTES = 256
SLOT_HEADER = struct.Struct('<8Q')
CONFIG = struct.Struct('<8s6I')


@dataclass(frozen=True)
class Frame:
    sequence: int
    compositor_frame: int
    predicted_display_ns: int
    capture_begin_ns: int
    gpu_ready_ns: int
    published_ns: int
    host_observed_ns: int
    host_copy_ns: int
    width: int
    height: int
    rgba: bytes
    eye_count: int = 1  # Historical mono recordings remain readable.

    @property
    def eye_width(self):
        return self.width // self.eye_count

    def _eye_index(self, eye):
        if eye not in ('left', 'right'):
            raise ValueError('eye must be left or right')
        index = 0 if eye == 'left' else 1
        if index >= self.eye_count:
            raise ValueError('recording does not contain the right eye')
        return index

    def image(self, eye=None):
        from PIL import Image
        image = Image.frombytes('RGBX', (self.width, self.height), self.rgba)
        if eye is not None:
            x = self._eye_index(eye) * self.eye_width
            image = image.crop((x, 0, x + self.eye_width, self.height))
        return image.convert('RGB')

    def array(self, eye=None):
        import numpy as np
        pixels = np.frombuffer(self.rgba, dtype=np.uint8).reshape(self.height, self.width, 4)
        if eye is not None:
            x = self._eye_index(eye) * self.eye_width
            pixels = pixels[:, x:x+self.eye_width]
        return pixels[:, :, :3]

    @property
    def left(self):
        return self.array('left')

    @property
    def right(self):
        return self.array('right')

    def metadata(self):
        return {key: value for key, value in vars(self).items() if key != 'rgba'}


class FrameRing:
    def __init__(self, path, width=960, height=1080, slots=8, fps=90):
        if platform.machine() != 'x86_64':
            raise ValueError('ring v2 is qualified on little-endian x86_64 only')
        if any(type(v) is not int for v in (width, height, slots)):
            raise ValueError('dimensions/slots must be integers')
        if not (16 <= width <= 4096 and 16 <= height <= 4096 and 2 <= slots <= 64):
            raise ValueError('invalid ring dimensions or slot count')
        if width * height % 16 or not 1 <= fps <= 240:
            raise ValueError('pixel count must be divisible by 16; fps must be in 1..240')
        self.path = Path(path)
        self.width, self.height, self.slots = width * 2, height, slots
        self.slot_bytes = 64 + self.width * height * 4
        self.size = HEADER_BYTES + slots * self.slot_bytes
        # Cluster Conda libc/Python lack memfd wrappers; x86_64 Linux ABI above.
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        self.fd = libc.syscall(319, b'vr-observations', 3)  # CLOEXEC | ALLOW_SEALING
        if self.fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        try:
            os.fchmod(self.fd, 0o600)
            os.ftruncate(self.fd, self.size)
            # Guest root can access its donated FD. Seal its size so truncation
            # cannot SIGBUS the host reader or expand the agreed memory budget.
            fcntl.fcntl(self.fd, 1033, 7)  # F_ADD_SEALS: SEAL | SHRINK | GROW
            os.symlink(f'/proc/{os.getpid()}/fd/{self.fd}', self.path)
            self.map = mmap.mmap(self.fd, self.size)
        except BaseException:
            os.close(self.fd)
            raise
        CONFIG.pack_into(self.map, 0, b'VRRING2\0', 2, self.width, height, slots, self.slot_bytes, HEADER_BYTES)
        struct.pack_into('<3Q', self.map, 32, 0, int(1e9/fps), 1)

    def latest(self, after=0, timeout=5):
        deadline = time.monotonic() + timeout
        while True:
            sequence = struct.unpack_from('<Q', self.map, 32)[0]
            if sequence > after:
                offset = HEADER_BYTES + ((sequence - 1) % self.slots) * self.slot_bytes
                meta = SLOT_HEADER.unpack_from(self.map, offset)
                if meta[0] == sequence * 2 and meta[1] == sequence and meta[7] == 2:
                    begin = time.monotonic_ns()
                    pixels = self.map[offset + 64:offset + self.slot_bytes]
                    done = time.monotonic_ns()
                    # Writer may have lapped us during the copy. Reject torn data.
                    if struct.unpack_from('<Q', self.map, offset)[0] == sequence * 2:
                        return Frame(sequence, *meta[2:7], done, done-begin,
                                     self.width, self.height, pixels, eye_count=2)
            if time.monotonic() >= deadline:
                raise TimeoutError('no complete new VR frame before deadline')
            time.sleep(.0005)

    def close(self):
        self.map.close()
        os.close(self.fd)
        self.path.unlink(missing_ok=True)


class VRStream:
    def __init__(self, name, *, manager=None, width=960, height=1080, fps=90,
                 hz=120, mirror='none', slots=8, x11_display=':1',
                 xauthority='/home/ga/.Xauthority', start_game=True):
        if not name.startswith('vr-'):
            raise ValueError('use a disposable VR sandbox named vr-*')
        if hz not in (60, 72, 90, 120, 144) or mirror not in ('none', 'pbo', 'sync'):
            raise ValueError('invalid compositor rate or mirror mode')
        if not re.fullmatch(r':[0-9]+(?:\.[0-9]+)?', x11_display) or not xauthority.startswith('/'):
            raise ValueError('select a local X11 display and absolute guest Xauthority path')
        self.manager, self.name = manager or EnvironmentManager(), name
        self.options = dict(width=width, height=height, fps=fps, slots=slots)
        self.hz, self.mirror = hz, mirror
        self.x11_display, self.xauthority = x11_display, xauthority
        # False lets the caller launch/stop a different XR game after the
        # runtime and input bridge are ready. It must stop that game on exit.
        self.start_game = start_game
        self.monado = self.bridge = self.ring = self.owner = self.lifecycle = None
        self.started_game = False
        self.broken = False
        self.sequence = 0
        self.input_mutex = threading.Lock()
        self.closed = False

    def guest(self, *args, **kwargs):
        return self.manager._run([*self.manager._command(self.name), 'exec', self.name, *args], **kwargs)

    def __enter__(self):
        try:
            self._start()
            return self
        except BaseException:
            self.close()
            raise

    def _start(self):
        state = self.manager.status(self.name)
        if state['status'] != 'running' or not state.get('gpu'):
            raise ValueError('VR stream needs a running GPU sandbox')
        self.lifecycle = io_lock(self.manager, self.name)
        self.lifecycle.__enter__()
        self.directory = self.manager.local / 'gvisor/vr-io' / self.name
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner = (self.directory/'owner.lock').open('a+')
        fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if self.guest('sh', '-c', 'pgrep -x monado-service || true').strip():
            raise RuntimeError('Monado is running: stop the VR experiment before opening a stream')
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode='w') as tar:
            for name in ('vr_input.py', 'vr_stream_guest.py', 'vr-remote-input.py', 'vr-monado-guest.sh'):
                tar.add(self.manager.lab/'scripts'/name, arcname=name)
        subprocess.run([*self.manager._command(self.name), 'exec', self.name, 'sh', '-c',
                        'umask 022; tar -xf - -C /opt/vr; chmod a+r /opt/vr/*.py /opt/vr/vr-monado-guest.sh'],
                       input=payload.getvalue(), capture_output=True, check=True, timeout=15)
        self.guest('test', '!', '-e', '/dev/kvm')
        self.guest('rm', '-f', '/run/user/1000/monado_comp_ipc')
        generation = uuid.uuid4().hex
        self.ring = FrameRing(self.directory/(generation+'.ring'), **self.options)
        self.pid_path = self.directory/(generation+'.pid')
        self.log_path = self.manager.lab/'runs/vr'/self.name/(generation+'.stream.log')
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        command = self.manager._command(self.name)
        command += ['exec', '--pass-fd='+str(self.ring.fd)+':3',
                    '--internal-pid-file=/local/'+str(self.pid_path.relative_to(self.manager.local)),
                    '--env=XRT_LAB_STREAM_FD=3', '--env=VR_HZ='+str(self.hz), self.name,
                    'runuser', '-u', 'ga', '--', '/usr/local/bin/engine-gpu', 'sh', '/opt/vr/vr-monado-guest.sh', 'monado']
        self.monado = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(self.ring.fd,))
        def drain_log():
            with self.log_path.open('wb') as log:
                while chunk := self.monado.stdout.read(8192):
                    log.write(chunk)
                    log.flush()
        self.log_thread = threading.Thread(target=drain_log, daemon=True)
        self.log_thread.start()
        prefix = [*self.manager._command(self.name), 'exec', '--user=1000:1000', self.name]
        self.bridge = subprocess.Popen([*prefix, 'python3', '-u', '/opt/vr/vr_stream_guest.py'],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=None, bufsize=0, start_new_session=True)
        self.bridge_buffer = bytearray()
        if not self._reply(timeout=35).get('ready'):
            raise RuntimeError('VR input bridge did not initialize')
        # The clock domains may have different epochs. Bound their offset with
        # the lowest round-trip ping, not a cross-domain subtraction assumption.
        samples = []
        for _ in range(20):
            sent = time.monotonic_ns()
            reply = self._request({'op': 'ping'})
            received = time.monotonic_ns()
            samples.append((received-sent, (sent+received)//2-reply['guest_ns']))
        self.clock_rtt_ns, self.guest_to_host_ns = min(samples)
        if self.start_game:
            self.guest('systemd-run', '--unit=vr-open-saber-live', '--uid=ga', '--collect',
                       '--setenv=VGL_READBACK='+self.mirror,
                       '--setenv=VR_X11_DISPLAY='+self.x11_display,
                       '--setenv=VR_XAUTHORITY='+self.xauthority, '/usr/local/bin/engine-gpu-gl',
                       'sh', '/opt/vr/vr-monado-guest.sh', 'game')
            self.started_game = True
            self.ring.latest(timeout=30)

    def _reply(self, timeout=5):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.bridge_buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.bridge.stdout], [], [], remaining)[0]:
                raise TimeoutError('VR input timed out; delivery outcome unknown')
            chunk = os.read(self.bridge.stdout.fileno(), 65536)
            if not chunk:
                raise ConnectionError('VR input disconnected; delivery outcome unknown')
            self.bridge_buffer.extend(chunk)
            if len(self.bridge_buffer) > 65536:
                raise ValueError('invalid VR input reply length')
        line, _, rest = self.bridge_buffer.partition(b'\n')
        self.bridge_buffer = bytearray(rest)
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ValueError('invalid VR input reply')
        return reply

    def _request(self, request):
        if self.broken:
            raise RuntimeError('input channel failed; actions cannot be replayed safely')
        payload = json.dumps(request, separators=(',', ':'), allow_nan=False).encode()+b'\n'
        if len(payload) > 65536:
            raise ValueError('VR input message exceeds 64 KiB')
        try:
            view = memoryview(payload)
            deadline = time.monotonic()+5
            fd = self.bridge.stdin.fileno()
            os.set_blocking(fd, False)
            while view:
                if not select.select([], [fd], [], max(0, deadline-time.monotonic()))[1]:
                    raise TimeoutError('input write timed out; delivery outcome unknown')
                try:
                    view = view[os.write(fd, view):]
                except BlockingIOError:
                    continue
            reply = self._reply()
        except (OSError, ConnectionError, TimeoutError, ValueError):
            self.broken = True
            raise
        if 'error' in reply:
            raise ValueError(reply['error'])
        return reply

    def input(self, state):
        with self.input_mutex:
            self.sequence += 1
            sent = time.monotonic_ns()
            reply = self._request({'op': 'input', 'id': self.sequence, 'state': state})
            received = time.monotonic_ns()
            if reply.get('id') != self.sequence:
                self.broken = True
                raise ConnectionError('input acknowledgement ID mismatch')
            return {**reply, 'host_sent_ns': sent, 'host_ack_ns': received,
                    'round_trip_ns': received-sent}

    def latest(self, after=0, timeout=5):
        return self.ring.latest(after, timeout)

    def close(self):
        if self.closed:
            return
        try:
            if self.bridge is not None:
                self.bridge.stdin.close()
                try:
                    self.bridge.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Stop our Monado below; that closes the stalled socket.
                    pass
            if self.started_game:
                self.guest('systemctl', 'stop', 'vr-open-saber-live')
                self.started_game = False
            if self.monado is not None:
                if self.monado.poll() is None:
                    pid = int(self.pid_path.read_text())
                    self.guest('kill', '-TERM', str(pid))
                    try:
                        self.monado.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        self.guest('kill', '-KILL', str(pid))
                        self.monado.wait(timeout=5)
                self.log_thread.join(timeout=5)
                self.monado.stdout.close()
                self.pid_path.unlink(missing_ok=True)
            if self.bridge is not None:
                self.bridge.wait(timeout=5)
                self.bridge.stdout.close()
            if self.ring is not None:
                self.ring.close()
            self.closed = True
        finally:
            if self.owner is not None:
                self.owner.close()
            if self.lifecycle is not None:
                self.lifecycle.__exit__(None, None, None)
                self.lifecycle = None

    def __exit__(self, *args):
        self.close()


class FrameRecorder:
    """Bounded background lossless RGBX/Zstandard images with an indexed timeline."""
    def __init__(self, directory, *, capacity=32, workers=4):
        import zstandard  # Fail before starting worker threads if unavailable.
        if not (1 <= capacity <= 1024 and 1 <= workers <= 32):
            raise ValueError('invalid recorder capacity or worker count')
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.queue = queue.Queue(capacity)
        self.saved = self.dropped = 0
        self.closed = False
        self.errors = []
        self.mutex = threading.Lock()
        self.index = (self.directory/'frames.jsonl').open('w')
        self.threads = [threading.Thread(target=self._worker, daemon=True) for _ in range(workers)]
        for thread in self.threads:
            thread.start()

    def submit(self, frame):
        if self.closed:
            raise RuntimeError('recorder is closed')
        try:
            self.queue.put_nowait(frame)
        except queue.Full:
            self.dropped += 1

    def _worker(self):
        import zstandard
        compressor = zstandard.ZstdCompressor(level=1)
        while (frame := self.queue.get()) is not None:
            try:
                name = f'{frame.sequence:010d}.rgba.zst'
                begin = time.monotonic_ns()
                encoded = compressor.compress(frame.rgba)
                compressed = time.monotonic_ns()
                (self.directory/name).write_bytes(encoded)
                written = time.monotonic_ns()
                with self.mutex:
                    self.index.write(json.dumps({**frame.metadata(), 'file': name,
                                     'pixel_format': 'RGBX8', 'codec': 'zstd',
                                     'record_encode_ns': compressed-begin,
                                     'record_write_ns': written-compressed})+'\n')
                    self.saved += 1
            except Exception as error:
                with self.mutex:
                    if not self.errors:
                        self.errors.append(str(error))
            finally:
                self.queue.task_done()
        self.queue.task_done()

    def close(self):
        if self.closed:
            return
        self.closed = True
        for _ in self.threads:
            self.queue.put(None)
        for thread in self.threads:
            thread.join()
        self.index.close()
        if self.errors:
            raise RuntimeError('background recording failed: '+self.errors[0])

    def video(self, eye=None):
        if not self.closed:
            raise RuntimeError('close the recorder before exporting video')
        return RecordedFrames(self.directory).video(eye)


class RecordedFrames:
    """Indexed, lossless images; observation and input clocks are retained."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.metadata = sorted((json.loads(line) for line in (self.directory/'frames.jsonl').read_text().splitlines()),
                               key=lambda frame: frame['sequence'])

    def __len__(self):
        return len(self.metadata)

    def __getitem__(self, index):
        import zstandard
        meta = self.metadata[index]
        width, height = meta['width'], meta['height']
        eye_count = meta.get('eye_count', 1)
        if eye_count not in (1, 2) or width % eye_count or not (16 <= width//eye_count <= 4096 and 16 <= height <= 4096):
            raise ValueError('invalid recorded image dimensions')
        if meta['codec'] != 'zstd' or meta['pixel_format'] != 'RGBX8':
            raise ValueError('unsupported recorded image encoding')
        if Path(meta['file']).name != meta['file']:
            raise ValueError('recorded image filename must be local')
        raw = zstandard.ZstdDecompressor().decompress((self.directory/meta['file']).read_bytes(),
                                                     max_output_size=width*height*4)
        if len(raw) != width*height*4:
            raise ValueError('recorded image byte length mismatch')
        return Frame(**{key: meta[key] for key in Frame.__dataclass_fields__ if key not in ('rgba', 'eye_count')},
                     rgba=raw, eye_count=eye_count)

    def video(self, eye=None):
        if eye not in (None, 'left', 'right'):
            raise ValueError('eye must be left, right, or None for stereo')
        recording = self
        if len(recording) < 2:
            raise ValueError('video requires at least two recorded frames')
        target = (self.directory/('gameplay.mp4' if eye is None else eye+'-eye.mp4')).resolve()
        # PNG conversion and video encoding happen after live recording has
        # closed. Temporary previews are local and removed after encoding.
        with tempfile.TemporaryDirectory(prefix='vr-video-') as temporary:
            temporary = Path(temporary)
            def export(index):
                frame = recording[index]
                frame.image(eye).save(temporary/f'{index:010d}.png', compress_level=1)
            with ThreadPoolExecutor(max_workers=4) as pool:
                for _ in pool.map(export, range(len(recording))):
                    pass
            lines = ['ffconcat version 1.0']
            for i, meta in enumerate(recording.metadata):
                lines += [f"file '{i:010d}.png'", 'option framerate 1000']
                if i+1 < len(recording):
                    duration = (recording.metadata[i+1]['capture_begin_ns']-meta['capture_begin_ns'])/1e9
                    lines.append(f'duration {duration:.9f}')
            listing = temporary/'frames.ffconcat'
            listing.write_text('\n'.join(lines)+'\n')
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'concat', '-safe', '0',
                            '-i', str(listing), '-fps_mode', 'vfr', '-c:v', 'libx264',
                            '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p',
                            '-threads', '4', '-movflags', '+faststart', str(target)], check=True)
        return target
