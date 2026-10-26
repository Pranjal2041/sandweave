"""Guest-side audio capture for Modal sandboxes: the sound the guest plays, as stamped PCM.

guest_boot.py runs PulseAudio with one null sink (SINK), the default output for
every application. This captures that sink's monitor with parec into a ring of
the last RETAIN_SECONDS, indexed by absolute frame number, so a reader asks for
everything after its cursor and learns exactly how many frames it missed.
Each read is stamped on the guest CLOCK_MONOTONIC (the clock of VR and desktop
frame captures): end_ns is when the relay received the last returned frame;
PulseAudio captured it up to its record latency (LATENCY_MS requested) earlier.
"""
import os
from pathlib import Path
import pwd
import subprocess
import threading
import time

SINK = 'sandweave'
RATE, CHANNELS, SAMPLE_BYTES = 48000, 2, 2
FRAME_BYTES = CHANNELS * SAMPLE_BYTES
RETAIN_SECONDS = 60
LATENCY_MS = 10
LOGS = Path('/run/sandweave/logs')


class Capture:
    def __init__(self, user='ga', *, server=None, sink=SINK):
        """Capture `sink` of the guest's sound server, or of another PulseAudio server (`server`,
        for example a VR session supervisor's own, unix:<runtime>/pulse/native)."""
        LOGS.mkdir(parents=True, exist_ok=True)
        account = pwd.getpwnam(user)
        # PulseAudio authenticates clients with the cookie under the user's HOME.
        self.process = subprocess.Popen(
            ['runuser', '-u', user, '--', 'env', 'HOME=' + account.pw_dir,
             f'XDG_RUNTIME_DIR=/run/user/{account.pw_uid}', *(['PULSE_SERVER=' + server] if server else []),
             'parec', '--raw',
             '--device=' + sink + '.monitor', '--format=s16le', f'--rate={RATE}', f'--channels={CHANNELS}',
             f'--latency-msec={LATENCY_MS}', f'--process-time-msec={LATENCY_MS}', '--client-name=sandweave-capture'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=(LOGS / 'audio-capture.log').open('ab'),
            bufsize=0, start_new_session=True)
        self.condition = threading.Condition()
        self.buffer, self.base, self.frames = bytearray(), 0, 0
        self.received_ns, self.error = None, None
        self.thread = threading.Thread(target=self._read, name='sandweave-audio', daemon=True)
        self.thread.start()
        with self.condition:
            # The null sink renders continuously, so a working capture delivers within milliseconds.
            if not self.condition.wait_for(lambda: self.frames or self.error, timeout=10):
                raise TimeoutError('audio capture produced no samples')
            if self.error:
                raise RuntimeError(self.error)

    def _read(self):
        pending = b''
        try:
            while chunk := os.read(self.process.stdout.fileno(), 65536):
                received = time.monotonic_ns()
                pending += chunk
                whole = len(pending) - len(pending) % FRAME_BYTES
                with self.condition:
                    self.buffer += pending[:whole]
                    self.frames += whole // FRAME_BYTES
                    self.received_ns = received
                    excess = len(self.buffer) // FRAME_BYTES - RATE * RETAIN_SECONDS
                    if excess > 0:
                        del self.buffer[:excess * FRAME_BYTES]
                        self.base += excess
                    self.condition.notify_all()
                pending = pending[whole:]
            message = 'parec exited with status ' + str(self.process.wait())
        except BaseException as error:
            message = f'{type(error).__name__}: {error}'
        with self.condition:
            self.error = 'audio capture stopped: ' + message + ' ' + \
                (LOGS / 'audio-capture.log').read_text(errors='replace')[-1000:]
            self.condition.notify_all()

    def read(self, after):
        if type(after) is not int or after < 0:
            raise ValueError('audio cursor must be a non-negative frame index')
        with self.condition:
            if self.error:
                raise RuntimeError(self.error)
            if after > self.frames:
                raise ValueError('audio cursor is ahead of the capture')
            first, end, received = max(after, self.base), self.frames, self.received_ns
            pcm = bytes(self.buffer[(first - self.base) * FRAME_BYTES:(end - self.base) * FRAME_BYTES])
        frames = end - first
        return {'pcm': pcm, 'format': 's16le', 'rate': RATE, 'channels': CHANNELS,
                'first_frame': first, 'next': end, 'frames': frames, 'dropped_frames': first - after,
                'start_ns': received - frames * 10**9 // RATE, 'end_ns': received,
                'latency_ms_requested': LATENCY_MS}

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
