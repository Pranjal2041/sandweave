"""Tracked input and actual paired compositor images; no game-tick assumptions."""
from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
import uuid

from ...sandbox.asyncio import dualmethod
from ...sandbox.errors import UnsupportedFeature


@dataclass(frozen=True)
class VRObservation:
    left: object
    right: object
    metadata: dict


class VR:
    def __init__(self, sandbox, config):
        self.sandbox, self.config = sandbox, config
        self.time_state = None

    def _frame(self, method='observe', **parameters):
        from .frames import Frame
        # Modal sandboxes are remote: their guest encodes JPEG, forwarded unchanged.
        default = 'jpeg' if self.sandbox._info.get('spec', {}).get('runtime') == 'modal' else 'zstd'
        value = self.sandbox._call('control', name='vr', method=method,
                                  parameters={**parameters, 'codec': self.config.get('transport', default)})
        if value.get('codec') == 'jpeg':
            import io
            from PIL import Image
            frame = value['frame']
            frame['rgba'] = Image.open(io.BytesIO(frame['rgba'])).convert('RGBX').tobytes()
            if len(frame['rgba']) != frame['width'] * frame['height'] * 4:
                raise ValueError('invalid decoded VR frame length')
        elif value.get('codec') == 'zstd':
            import zstandard
            expected = value['frame']['width'] * value['frame']['height'] * 4
            if not 0 < expected <= 2 * 4096 * 4096 * 4:
                raise ValueError('invalid VR frame dimensions')
            value['frame']['rgba'] = zstandard.ZstdDecompressor().decompress(value['frame']['rgba'], max_output_size=expected)
            if len(value['frame']['rgba']) != expected:
                raise ValueError('invalid decoded VR frame length')
        return Frame(**value['frame']), value['metadata']

    @dualmethod
    def observe(self, *, after=0, timeout=5):
        frame, metadata = self._frame(after=after, timeout=timeout)
        return VRObservation(frame.left.copy(), frame.right.copy(), metadata)

    @dualmethod
    def step(self, action, *, timeout=5):
        frame, metadata = self._frame('step', action=action, timeout=timeout)
        return VRObservation(frame.left.copy(), frame.right.copy(), metadata)

    @dualmethod
    def action(self, action):
        return self.sandbox._call('control', name='vr', method='action', parameters={'action': action})

    @dualmethod
    def haptics(self, *, after=None):
        """Vibration commands the game sent to the virtual controllers since the previous call.

        Each event has device, guest_ns (guest monotonic receipt time), amplitude (0..1),
        frequency_hz (0: unspecified), duration_ns (-1: the runtime's minimum pulse) and stop
        (amplitude 0, recorded only when it ends a vibration). `after` re-reads from a record index.
        """
        return self.sandbox._call('control', name='vr', method='haptics', parameters={'after': after})

    @dualmethod
    def time(self, *, scale=None, advance_ticks=None, timeout=10):
        """Game-time control, for games that implement it (time_control in the template).

        scale=0 pauses (the frozen scene keeps rendering), 0 < scale <= 4 sets the speed,
        advance_ticks=N runs exactly N physics ticks from a pause and pauses again. Returns the
        game's acknowledgement: paused, time_scale, ticks, song_position, physics_ticks_per_second.
        """
        value = self.sandbox._call('control', name='vr', method='time',
                                   parameters={'scale': scale, 'advance_ticks': advance_ticks, 'timeout': timeout})
        self.time_state = value
        return value

    def record(self, directory, *, fps=30):
        return Recording(self, directory, fps=fps)


class Recording:
    def __init__(self, vr, directory, *, fps):
        if not isinstance(fps, (float, int)) or not 1 <= fps <= 90:
            raise ValueError('recording fps must be in 1..90')
        self.vr, self.directory, self.fps = vr, Path(directory), fps
        self.stop_event = threading.Event()
        self.errors, self.timeline = [], []
        self.recorder = self.thread = None
        self.closed = False
        self.metadata = None

    def __enter__(self):
        from .frames import FrameRecorder
        self.recorder = FrameRecorder(self.directory)

        def capture():
            sequence = 0
            try:
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    frame, metadata = self.vr._frame(after=sequence, timeout=5)
                    if frame.eye_count != 2:
                        raise RuntimeError('VR recording requires both eyes')
                    sequence = frame.sequence
                    self.timeline.append(frame.metadata())
                    self.recorder.submit(frame)
                    self.stop_event.wait(max(0, 1/self.fps - (time.monotonic()-started)))
            except BaseException as error:
                self.errors.append(error)
                self.stop_event.set()

        self.thread = threading.Thread(target=capture, name='sandweave-vr-recorder')
        self.thread.start()
        return self

    @dualmethod
    def close(self):
        if self.closed:
            return self.metadata
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise TimeoutError('VR capture did not stop; recording has not been finalized')
        if self.recorder:
            self.recorder.close()
        self.closed = True
        if self.errors:
            raise RuntimeError('VR recording failed: ' + str(self.errors[0])) from self.errors[0]
        if not self.recorder:
            return None
        videos = {eye or 'stereo': str(self.recorder.video(eye)) for eye in ('left', 'right', None)}
        duration = (self.timeline[-1]['capture_begin_ns'] - self.timeline[0]['capture_begin_ns'])/1e9
        self.metadata = {'eye_count': 2, 'frames_received': len(self.timeline), 'frames_saved': self.recorder.saved,
                         'recorder_dropped': self.recorder.dropped, 'requested_fps': self.fps,
                         'observed_fps': (len(self.timeline)-1)/duration if duration else None,
                         'duration_seconds': duration, 'videos': videos,
                         'ring_frames_skipped': self.timeline[-1]['sequence']-self.timeline[0]['sequence']+1-len(self.timeline),
                         'meaning': 'paired compositor capture cadence; application submission FPS is measured separately'}
        (self.directory / 'recording.json').write_text(json.dumps(self.metadata, indent=2) + '\n')
        return self.metadata

    def __exit__(self, *args):
        self.close()

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *args):
        await self.close.aio()


class AttachedVR:
    def __init__(self, context, config, cold):
        import zstandard
        self.compressor = zstandard.ZstdCompressor(level=1)
        self.context, self.time_control = context, config.get('time_control')
        self.haptics_cursor, self.haptics_lock = 0, threading.Lock()
        options = {k: config[k] for k in ('width', 'height', 'fps', 'hz', 'mirror', 'slots') if k in config}
        self.stream, self.session = None, config.get('session_agent')
        if self.session:
            # The environment starts its own VR session supervisor after boot (it owns Monado and
            # the game); the stream attaches to its agent socket on first use.
            if not hasattr(context.runtime, 'vr_stream'):
                raise UnsupportedFeature('VR session supervisors are relayed by runtime="modal"')
            self.options = {**options, 'session_agent': self.session, 'session_haptics': config.get('session_haptics')}
            self.stream_lock = threading.Lock()
            return
        if hasattr(context.runtime, 'vr_stream'):
            # A remote runtime owns Monado and the frame ring inside its sandbox.
            self.stream = context.runtime.vr_stream(context, **options)
        else:
            from vr_stream import VRStream
            self.stream = VRStream(context.id, manager=context.runtime.manager, start_game=False, **options)
        try:
            self.stream.__enter__()
            command = config['game_command']
            self.process_id = uuid.uuid4().hex
            context.worker.command_start(context.id, self.process_id,
                argv=command if isinstance(command, list) else None,
                command=command if isinstance(command, str) else None,
                user=config.get('game_user', 'ga'), env=config.get('game_env', {}),
                cwd=config.get('game_cwd', '/workspace'))
            context.worker.process_stdin(context.id, self.process_id, close=True)
            deadline = time.monotonic() + context.remaining(config.get('ready_timeout', 90))
            sequence = 0
            while True:
                state = context.worker.process_status(context.id, self.process_id)
                if state['returncode'] is not None:
                    error = context.worker.process_output(context.id, self.process_id, stream='stderr', size=4096)
                    raise RuntimeError('VR application exited during startup: ' + error.decode(errors='replace'))
                frame = self.stream.latest(after=sequence, timeout=max(.001, deadline-time.monotonic()))
                sequence = frame.sequence
                if frame.eye_count != 2:
                    raise RuntimeError('VR template did not produce both eye images')
                if not config.get('wait_for_content', True) or (frame.left.any() and frame.right.any()):
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('VR application did not paint both eyes before readiness deadline')
        except BaseException:
            self.stream.abort_attachment()
            raise

    def _session_stream(self):
        with self.stream_lock:
            if self.stream is None:
                stream = self.context.runtime.vr_stream(self.context, **self.options)
                stream.__enter__()
                self.stream = stream
        return self.stream

    def call(self, method, parameters):
        if self.session:
            self._session_stream()
        if method == 'action':
            return self.stream.input(parameters['action'])
        if method == 'time':
            if not hasattr(self.stream, 'game_time') or not self.time_control:
                raise UnsupportedFeature('game-time control needs runtime="modal" and a game that implements it')
            return self.stream.game_time(parameters.get('scale'), parameters.get('advance_ticks'),
                                         parameters.get('timeout', 10))
        if method == 'haptics':
            if not hasattr(self.stream, 'haptics'):
                raise UnsupportedFeature('VR haptics are recorded by runtime="modal"')
            after = parameters.get('after')
            with self.haptics_lock:
                value = self.stream.haptics(self.haptics_cursor if after is None else after)
                if after is None:
                    self.haptics_cursor = value['next']
            return value
        ack = None
        timeout = parameters.get('timeout', 5)
        if not 0 < timeout <= 120:
            raise ValueError('VR observation timeout must be in 0..120 seconds')
        if method == 'step':
            ack = self.stream.input(parameters['action'])
            after = 0
        elif method == 'observe':
            after = parameters.get('after', 0)
        else:
            raise UnsupportedFeature('unknown VR operation: ' + method)
        codec = parameters.get('codec', 'raw')
        if codec not in ('raw', 'zstd', 'jpeg'):
            raise ValueError('VR transport must be raw, zstd or jpeg')
        # A remote stream already holds guest-encoded JPEG: forward it without decoding.
        passthrough = codec == 'jpeg' and hasattr(self.stream, 'latest_encoded')
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(.001, deadline-time.monotonic())
            if passthrough:
                fields, payload, _ = self.stream.latest_encoded(after=after, timeout=remaining)
                frame = None
            else:
                frame = self.stream.latest(after=after, timeout=remaining)
                fields = {k: v for k, v in vars(frame).items() if k != 'rgba'}
            # Both timestamps come from CLOCK_MONOTONIC in the same guest.
            if ack is None or fields['capture_begin_ns'] >= ack['guest_ack_ns']:
                break
            after = fields['sequence']
            if time.monotonic() >= deadline:
                raise TimeoutError('no paired-eye capture after input acknowledgement')
        started = time.monotonic_ns()
        if codec == 'zstd':
            payload = self.compressor.compress(frame.rgba)
        elif codec == 'raw':
            payload = frame.rgba
        elif not passthrough:
            from PIL import Image
            import io
            output = io.BytesIO()
            Image.frombytes('RGBX', (frame.width, frame.height), frame.rgba).convert('RGB').save(output, 'JPEG', quality=90)
            payload = output.getvalue()
        return {'frame': {**fields, 'rgba': payload}, 'codec': codec,
                'metadata': {**fields, 'input': ack,
                'transport': {'codec': codec, 'bytes': len(payload), 'encode_ns': time.monotonic_ns()-started},
                'guest_to_host_ns': self.stream.guest_to_host_ns,
                'minimum_ping_rtt_ns': self.stream.clock_rtt_ns,
                'acknowledgement': 'runtime received state; no game-consumption or fixed-tick guarantee'}}

    def detach(self, reason):
        if self.stream is None:
            return reason in ('pause', 'snapshot')
        if reason in ('pause', 'snapshot'):
            self.stream.suspend_attachment()
            return True
        self.stream.abort_attachment()
        return False

    def reattach(self):
        if self.stream is not None:
            self.stream.resume_attachment()


class VRProvider:
    api_version = 1

    @staticmethod
    def descriptor(config):
        return {'version': 1, 'schema': 'monado.head-and-controllers.v1', 'eye_count': 2,
                'time_control': config.get('time_control'), 'session': bool(config.get('session_agent')),
                'state': ['filesystem'], 'unavailable': {'memory': 'live GPU graphics checkpoints are unsupported'},
                'acknowledgement': 'runtime state received; not a simulation step'}

    @staticmethod
    def bind(sandbox, config):
        return VR(sandbox, config)

    @staticmethod
    def attach(context, config, cold=False):
        return AttachedVR(context, config, cold)
