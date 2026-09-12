"""Worker-owned desktop recordings, separate from guest/snapshot storage.

A recorder process owns its capture connection and encoder. The worker can
restart without interrupting capture. Lifecycle operations finalize the current
segment before touching the guest. Fragmented MP4 retains completed fragments
if the recorder or its display disappears unexpectedly.
"""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import queue
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from ...sandbox.errors import UnsupportedFeature
from ...sandbox.ownership import process_identity, process_alive
from ...sandbox.recording import normalize
from ...sandbox.workspace import atomic_json

FILES = {'segment.json', 'timeline.jsonl', 'video.mp4', 'encoder.log'}


class Recordings:
    def __init__(self, worker):
        self.worker = worker
        self.children = {}

    def directory(self, identity):
        self.worker.path(identity)
        return self.worker.root / 'recordings' / identity

    def status(self, identity):
        for pid, child in list(self.children.items()):
            if child.poll() is not None:
                self.children.pop(pid, None)
        root = self.directory(identity)
        try:
            config = json.loads((root/'control.json').read_text())
        except FileNotFoundError:
            config = {}
        segments = []
        for file in sorted(root.glob('*/segment.json')):
            try:
                item = json.loads(file.read_text())
            except FileNotFoundError:
                continue
            if item['state'] in ('starting', 'recording') and process_alive(item.get('process')) is False:
                item.update(state='partial', error='recorder process exited before finalization')
            item.pop('process', None)
            segments.append(item)
        segments.sort(key=lambda s: s['started_at'])
        states = {s['state'] for s in segments}
        desired = config.get('desired', 'off')
        state = ('recording' if states & {'starting', 'recording'} else
                 'partial' if 'partial' in states else
                 'complete' if desired == 'recording' and segments else
                 'waiting' if desired == 'recording' else desired)
        return {'sandbox_id': identity, 'state': state, 'options': config.get('options'),
                'segments': segments, 'location': str(root),
                'frames_captured': sum(s.get('frames_captured', 0) for s in segments),
                'capture_slots_missed': sum(s.get('capture_slots_missed', 0) for s in segments),
                'encoder_queue_dropped': sum(s.get('encoder_queue_dropped', 0) for s in segments)}

    def start(self, identity, *, resume=False):
        record = self.worker.read(identity)
        options = normalize(record['spec'].get('recording', False))
        if options is None:
            return self.status(identity)
        root = self.directory(identity)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        control = root/'control.json'
        previous = json.loads(control.read_text()) if control.exists() else {}
        if resume and previous.get('desired') != 'paused':
            return self.status(identity)
        if previous and not resume:
            return self.status(identity)
        if self.status(identity)['state'] == 'recording':
            return self.status(identity)
        if record['state'] != 'ready':
            raise ValueError('recording requires a ready desktop')
        # All dependencies are prepared with the desktop, without installing
        # guest packages or needing guest internet access.
        from Crypto.Cipher import DES
        import imageio_ffmpeg
        encoder = imageio_ffmpeg.get_ffmpeg_exe()
        runtime = self.worker.runtime.status(identity)
        port = runtime.get('ports', {}).get('5901')
        if port is None:
            raise UnsupportedFeature('desktop recording requires an Xvnc endpoint')
        from ..controls import Context
        context = Context(self.worker, identity)
        encrypted = context.files.read_bytes('/home/ga/.vnc/passwd')[:8]
        if len(encrypted) != 8:
            raise ValueError('invalid Xvnc password file')
        # TigerVNC's d3des uses least-significant-bit-first key bytes. Convert
        # its fixed password-file key to the standard DES bit order.
        password = DES.new(bytes.fromhex('e84ad660c4721ae0'), DES.MODE_ECB).decrypt(encrypted).rstrip(b'\0')
        segment = root/uuid.uuid4().hex
        segment.mkdir(mode=0o700)
        atomic_json(control, {'desired': 'recording', 'options': options})
        command = [sys.executable, '-m', __name__, str(segment)]
        environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[3]) +
                       os.pathsep + os.environ.get('PYTHONPATH', ''),
                       'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1'}
        with (segment/'recorder.log').open('ab') as log:
            child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log,
                                     start_new_session=True, env=environment)
        self.children[child.pid] = child
        try:
            child.stdin.write(json.dumps({'port': int(port), 'password': password.hex(),
                                          'encoder': encoder, 'workspace': str(self.worker.root),
                                          'identity': identity, **options}).encode())
            child.stdin.close()
            deadline = time.monotonic() + 20
            while True:
                if child.poll() is not None:
                    raise RuntimeError('desktop recorder could not start: ' + (segment/'recorder.log').read_text()[-2000:])
                if (segment/'segment.json').exists():
                    state = json.loads((segment/'segment.json').read_text())
                    if state['state'] == 'recording':
                        return self.status(identity)
                    if state['state'] == 'partial':
                        raise RuntimeError('desktop recorder could not start: ' + state.get('error', 'unknown error'))
                if time.monotonic() >= deadline:
                    raise TimeoutError('desktop recording did not receive its first frame')
                time.sleep(.02)
        except BaseException:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=20)
            self.children.pop(child.pid, None)
            raise

    def stop(self, identity, *, reason='stopped'):
        root = self.directory(identity)
        control = root/'control.json'
        if not control.exists():
            return self.status(identity)
        config = json.loads(control.read_text())
        # Pausing a manually stopped recording must not enable it on resume.
        desired = reason if config['desired'] in ('recording', 'paused') else config['desired']
        atomic_json(control, {**config, 'desired': desired})
        for file in root.glob('*/segment.json'):
            item = json.loads(file.read_text())
            if item['state'] not in ('starting', 'recording'):
                continue
            process = item.get('process')
            if process_alive(process) is True:
                # pidfd prevents signaling an unrelated process if the recorder
                # exits and its PID is reused between inspection and signaling.
                manager = self.worker.runtime.adapter('gvisor').manager
                manager._signal(process['pid'], int(process['started']), signal.SIGTERM)
                deadline = time.monotonic()+30
                while process_alive(process) is True and time.monotonic() < deadline:
                    time.sleep(.05)
                if process_alive(process) is True:
                    manager._signal(process['pid'], int(process['started']), signal.SIGKILL)
                    deadline = time.monotonic()+5
                    while process_alive(process) is True and time.monotonic() < deadline:
                        time.sleep(.05)
                    if process_alive(process) is True:
                        raise TimeoutError('recorder did not stop; guest cleanup has not begun')
        self._wait_stopped(root)
        return self.status(identity)

    @staticmethod
    def _wait_stopped(root):
        # The encoder inherits this lease. Even if its recorder is killed, an
        # export/deletion waits for the final encoder writes to finish.
        deadline = time.monotonic()+30
        for path in root.glob('*/active.lock'):
            with path.open('a') as lease:
                while True:
                    try:
                        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('recording encoder is still finalizing; files have been retained')
                        time.sleep(.05)

    def dispatch(self, identity, action, **parameters):
        root = self.directory(identity)
        if action == 'status':
            return self.status(identity)
        if action == 'start':
            return self.start(identity)
        if action == 'stop':
            return self.stop(identity)
        if action == 'delete':
            if self.status(identity)['state'] == 'recording':
                raise ValueError('stop recording before deleting its files')
            self._wait_stopped(root)
            if root.exists():
                shutil.rmtree(root)
            return {'deleted': True}
        if action == 'manifest':
            status = self.status(identity)
            if not status['segments']:
                raise FileNotFoundError('this sandbox has no recording')
            if status['state'] == 'recording':
                raise ValueError('stop recording before exporting its files')
            self._wait_stopped(root)
            files = []
            for segment in status['segments']:
                for name in sorted(FILES):
                    path = root/segment['id']/name
                    if path.is_file():
                        digest = hashlib.sha256()
                        with path.open('rb') as source:
                            for block in iter(lambda: source.read(1024**2), b''):
                                digest.update(block)
                        files.append({'path': str(path.relative_to(root)), 'size': path.stat().st_size,
                                      'sha256': digest.hexdigest()})
            return {**status, 'files': files}
        if action == 'read':
            path = parameters.get('path', '')
            if not re.fullmatch(r'[0-9a-f]{32}/[a-z0-9.]+', path) or Path(path).name not in FILES:
                raise ValueError('invalid recording file')
            size, offset = parameters.get('size', 4*1024**2), parameters.get('offset', 0)
            if type(size) is not int or type(offset) is not int or not 0 <= size <= 4*1024**2 or offset < 0:
                raise ValueError('invalid recording read bounds')
            with (root/path).open('rb') as source:
                source.seek(offset)
                return source.read(size)
        raise ValueError('unknown recording operation: ' + action)


def encode_command(encoder, size, fps, path):
    return [encoder, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{size[0]}x{size[1]}',
            '-framerate', str(fps), '-i', 'pipe:0', '-an', '-c:v', 'libx264',
            '-threads', '1', '-preset', 'ultrafast', '-crf', '23',
            '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-pix_fmt', 'yuv420p',
            '-g', str(fps), '-movflags', '+frag_keyframe+empty_moov+default_base_moof',
            '-progress', str(path.parent/'progress.txt'), '-f', 'mp4', str(path)]


def run(directory, options, *, capture_factory=None):
    with (Path(directory)/'active.lock').open('a') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _run(directory, options, capture_factory=capture_factory, lease_fd=lease.fileno())


class PlainCapture:
    """Independent screenshot connection; Xvnc's framebuffer excludes the cursor."""
    def __init__(self, options):
        from ...sandbox.runtimes.router import Runtime
        self.client = Runtime(options['workspace']).adapter('gvisor').manager.fast_io(options['identity'])

    def frame(self):
        started = time.monotonic_ns()
        image = self.client.screenshot()
        return image, {'capture_started_ns': started, 'capture_completed_ns': time.monotonic_ns(),
                       'clock': 'worker CLOCK_MONOTONIC',
                       'meaning': 'display request/response interval; not application render time'}

    def close(self):
        self.client.close()


def _run(directory, options, *, capture_factory=None, lease_fd):
    from .rfb import Capture
    directory = Path(directory)
    stop = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    capture, encoder, writer = None, None, None
    pending = queue.Queue(maxsize=2)
    errors = []
    start = time.monotonic_ns()
    state = {'id': directory.name, 'state': 'starting', 'process': process_identity(),
             'started_at': time.time(), 'started_ns': start, 'requested_fps': options['fps'],
             'cursor': options['cursor'], 'frames_captured': 0, 'capture_slots_missed': 0,
             'encoder_queue_dropped': 0, 'frames_submitted': 0, 'frames_repeated': 0,
             'format': 'fragmented MP4, H.264; last incomplete fragment may be lost after a crash'}
    atomic_json(directory/'segment.json', state)

    def save():
        atomic_json(directory/'segment.json', {**state, 'updated_at': time.time()})

    def write_frames(first_size):
        try:
            with selectors.DefaultSelector() as selector, (directory/'timeline.jsonl').open('w', buffering=1) as timeline:
                os.set_blocking(encoder.stdin.fileno(), False)
                selector.register(encoder.stdin, selectors.EVENT_WRITE)
                previous_pixels = None
                while True:
                    item = pending.get()
                    if item is None:
                        break
                    image, metadata, slot = item
                    metadata = {**metadata, 'source_size': list(image.size)}
                    if image.size != first_size:
                        from PIL import Image, ImageOps
                        fitted = ImageOps.contain(image, first_size)
                        image = Image.new('RGB', first_size)
                        image.paste(fitted, ((first_size[0]-fitted.width)//2, (first_size[1]-fitted.height)//2))
                    pixels = image.tobytes()
                    if previous_pixels is None:
                        # Exclude setup/encoder startup from the video origin.
                        origin = slot
                    wanted = slot-origin
                    while state['frames_submitted'] <= wanted:
                        repeated = state['frames_submitted'] < wanted
                        view = memoryview(previous_pixels if repeated and previous_pixels is not None else pixels)
                        deadline = time.monotonic() + 10
                        while view:
                            if encoder.poll() is not None:
                                raise RuntimeError('video encoder exited; see encoder.log')
                            if not selector.select(max(0, deadline-time.monotonic())):
                                raise TimeoutError('video encoder stopped accepting frames')
                            try:
                                written = os.write(encoder.stdin.fileno(), view)
                            except BlockingIOError:
                                continue
                            view = view[written:]
                        state['frames_submitted'] += 1
                        state['frames_repeated'] += int(repeated)
                    timeline.write(json.dumps({**metadata, 'video_frame': wanted,
                                               'video_time_seconds': wanted/options['fps']}) + '\n')
                    previous_pixels = pixels
        except BaseException as error:
            errors.append(str(error))
            stop.set()
        finally:
            encoder.stdin.close()

    try:
        capture = (PlainCapture(options) if capture_factory is None and not options['cursor'] else
                   (capture_factory or Capture)(options['port'], bytes.fromhex(options['password']), cursor=options['cursor']))
        image, metadata = capture.frame()
        state['started_at'] += (metadata['capture_started_ns']-start)/1e9
        start = metadata['capture_started_ns']
        state.update(started_ns=start, width=image.width, height=image.height)
        with (directory/'encoder.log').open('wb') as log:
            encoder = subprocess.Popen(encode_command(options['encoder'], image.size, options['fps'], directory/'video.mp4'),
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                                       pass_fds=(lease_fd,))
        writer = threading.Thread(target=write_frames, args=(image.size,), daemon=True)
        writer.start()
        slot, next_slot, saved_at = 0, 0, 0
        period = 1e9/options['fps']
        while not stop.is_set():
            state['capture_slots_missed'] += max(0, slot-next_slot)
            state['frames_captured'] += 1
            span = (metadata['capture_started_ns']-start)/1e9
            state['capture_span_seconds'] = span
            state['observed_capture_fps'] = (state['frames_captured']-1)/span if span > 0 else None
            try:
                pending.put_nowait((image, metadata, slot))
            except queue.Full:
                state['encoder_queue_dropped'] += 1
            state['state'] = 'recording'
            if time.monotonic()-saved_at >= 1:
                save()
                saved_at = time.monotonic()
            next_slot = slot+1
            if stop.wait(max(0, (start+next_slot*period-time.monotonic_ns())/1e9)):
                break
            slot = max(next_slot, int((time.monotonic_ns()-start)/period))
            image, metadata = capture.frame()
    except BaseException as error:
        if not stop.is_set():
            errors.append(str(error))
    finally:
        stop.set()
        if capture is not None:
            try:
                capture.close()
            except Exception as error:
                errors.append('capture cleanup: ' + str(error))
        if writer is not None:
            while writer.is_alive():
                try:
                    pending.put(None, timeout=.1)
                    break
                except queue.Full:
                    continue
            writer.join(timeout=15)
        if encoder is not None:
            try:
                code = encoder.wait(timeout=15)
            except subprocess.TimeoutExpired:
                encoder.kill()
                code = encoder.wait()
            if code:
                errors.append('video encoder exited with ' + str(code))
            progress = directory/'progress.txt'
            if progress.exists():
                counts = re.findall(r'^frame=(\d+)$', progress.read_text(), re.M)
                if counts:
                    state['frames_encoded'] = int(counts[-1])
                    state['video_duration_seconds'] = state['frames_encoded']/options['fps']
        for name in ('video.mp4', 'timeline.jsonl'):
            if (directory/name).exists():
                try:
                    with (directory/name).open('rb') as output:
                        os.fsync(output.fileno())
                except OSError as error:
                    errors.append('could not sync ' + name + ': ' + str(error))
        state.update(state='partial' if errors else 'complete', ended_at=time.time(), ended_ns=time.monotonic_ns())
        if errors:
            state['error'] = '; '.join(errors)
        save()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return state


if __name__ == '__main__':
    run(Path(sys.argv[1]), json.load(sys.stdin))
