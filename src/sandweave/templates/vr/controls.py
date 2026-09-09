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

    def _frame(self, method='observe', **parameters):
        from .frames import Frame
        value = self.sandbox._call('control', name='vr', method=method, parameters=parameters)
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
        from vr_stream import VRStream
        self.context = context
        self.stream = VRStream(context.id, manager=context.runtime.manager, start_game=False,
                               **{k: config[k] for k in ('width', 'height', 'fps', 'hz', 'mirror', 'slots') if k in config})
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
            frame = self.stream.latest(timeout=config.get('ready_timeout', 90))
            if frame.eye_count != 2:
                raise RuntimeError('VR template did not produce both eye images')
        except BaseException:
            self.stream.abort_attachment()
            raise

    def call(self, method, parameters):
        if method == 'action':
            return self.stream.input(parameters['action'])
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
        deadline = time.monotonic() + timeout
        while True:
            frame = self.stream.latest(after=after, timeout=max(.001, deadline-time.monotonic()))
            # Both timestamps come from CLOCK_MONOTONIC in the same guest.
            if ack is None or frame.capture_begin_ns >= ack['guest_ack_ns']:
                break
            after = frame.sequence
            if time.monotonic() >= deadline:
                raise TimeoutError('no paired-eye capture after input acknowledgement')
        return {'frame': vars(frame), 'metadata': {**frame.metadata(), 'input': ack,
                'guest_to_host_ns': self.stream.guest_to_host_ns,
                'minimum_ping_rtt_ns': self.stream.clock_rtt_ns,
                'acknowledgement': 'runtime received state; no game-consumption or fixed-tick guarantee'}}

    def detach(self, reason):
        if reason in ('pause', 'snapshot'):
            self.stream.suspend_attachment()
            return True
        self.stream.abort_attachment()
        return False

    def reattach(self):
        self.stream.resume_attachment()


class VRProvider:
    api_version = 1

    @staticmethod
    def descriptor(config):
        return {'version': 1, 'schema': 'monado.head-and-controllers.v1', 'eye_count': 2,
                'state': ['filesystem'], 'unavailable': {'memory': 'live GPU graphics checkpoints are unsupported'},
                'acknowledgement': 'runtime state received; not a simulation step'}

    @staticmethod
    def bind(sandbox, config):
        return VR(sandbox, config)

    @staticmethod
    def attach(context, config, cold=False):
        return AttachedVR(context, config, cold)
