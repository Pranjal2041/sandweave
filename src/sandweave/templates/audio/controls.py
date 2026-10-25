"""The guest's audio output as PCM stamped on the guest clock, read since a cursor.

The runtime runs the sound server and captures its default output (runtime="modal":
PulseAudio's null sink, see runtimes/modal/guest_audio.py). observe() returns every
frame since the previous observe(), so successive observations tile the stream; a
reader slower than the guest's retained window learns how many frames it missed.
"""
import threading

from ...sandbox.asyncio import dualmethod
from ...sandbox.errors import UnsupportedFeature


class Audio:
    def __init__(self, sandbox, config):
        self.sandbox = sandbox

    @dualmethod
    def observe(self, *, after=None):
        """{'pcm': bytes, 'format': 's16le', 'rate', 'channels', 'first_frame', 'next', 'frames',
        'dropped_frames', 'start_ns', 'end_ns'}; end_ns is when the guest received the last frame.
        `after` re-reads from an absolute frame index instead of the previous observation."""
        return self.sandbox._call('control', name='audio', method='observe', parameters={'after': after})


class AttachedAudio:
    def __init__(self, context, config):
        if not hasattr(context.runtime, 'audio_stream'):
            raise UnsupportedFeature('audio capture is provided by runtime="modal"')
        self.connection = context.runtime.audio_stream(context, user=config.get('user', 'ga'))
        self.cursor, self.lock = 0, threading.Lock()

    def call(self, method, parameters):
        if method != 'observe':
            raise UnsupportedFeature('unknown audio operation: ' + method)
        after = parameters.get('after')
        with self.lock:
            value = self.connection.call('audio_read', after=self.cursor if after is None else after)
            if after is None:
                self.cursor = value['next']
        return value

    def detach(self, reason):
        self.connection.close()
        return False


class AudioProvider:
    api_version = 1

    @staticmethod
    def descriptor(config):
        return {'version': 1, 'schema': 'pcm.s16le-48k-stereo.v1', 'state': [],
                'clock': 'guest CLOCK_MONOTONIC, shared with frame captures'}

    @staticmethod
    def bind(sandbox, config):
        return Audio(sandbox, config)

    @staticmethod
    def attach(context, config, cold=False):
        return AttachedAudio(context, config)
