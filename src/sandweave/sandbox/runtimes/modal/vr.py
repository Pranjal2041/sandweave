"""Worker side of the Modal VR stream, with the same interface AttachedVR uses for VRStream."""
import time
import uuid

from ...errors import OperationUnknown


class RemoteVRStream:
    """Starts guest_vr.py through the guest agent and reads frames/input over its TLS tunnel."""

    def __init__(self, runtime, context, *, width=960, height=1080, fps=90, hz=90, mirror='none', slots=8):
        if hz not in (60, 72, 90, 120, 144) or mirror not in ('none', 'pbo', 'sync'):
            raise ValueError('invalid compositor rate or mirror mode')
        self.runtime, self.context = runtime, context
        self.options = dict(width=width, height=height, fps=fps, hz=hz, slots=slots)
        self.process_id = self.connection = None
        self.sequence = 0
        self.broken = self.closed = False
        self.clock_rtt_ns = self.guest_to_host_ns = None

    def _call(self, op, **parameters):
        return self.connection.call(op, **parameters)

    def __enter__(self):
        worker, identity = self.context.worker, self.context.id
        self.process_id = uuid.uuid4().hex
        argv = ['python3', '-u', '/opt/sandweave/guest_vr.py', '23800', '/run/sandweave/agent.token']
        for key, value in self.options.items():
            argv += ['--' + key, str(value)]
        worker.command_start(identity, self.process_id, argv=argv, user='root', cwd='/')
        worker.process_stdin(identity, self.process_id, close=True)
        self.connection = self.runtime.vr_connection(identity, timeout=60)
        deadline = time.monotonic() + self.context.remaining(60)
        while True:
            state = worker.process_status(identity, self.process_id)
            if state['returncode'] is not None:
                error = worker.process_output(identity, self.process_id, stream='stderr', size=8192)
                raise RuntimeError('guest VR stream exited during startup: ' + error.decode(errors='replace'))
            try:
                self._call('ping')
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise TimeoutError('guest VR stream did not become reachable')
                time.sleep(.5)
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
        if self.broken:
            raise RuntimeError('input channel failed; actions cannot be replayed safely')
        self.sequence += 1
        sent = time.monotonic_ns()
        try:
            reply = self._call('input', request={'op': 'input', 'id': self.sequence, 'state': state})
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

    def latest(self, after=0, timeout=5):
        import zstandard
        from vr_frames import Frame
        value = self._call('latest', after=after, timeout=timeout)
        expected = value['width'] * value['height'] * 4
        rgba = zstandard.ZstdDecompressor().decompress(value.pop('rgba'), max_output_size=expected)
        if len(rgba) != expected:
            raise ValueError('invalid decoded VR frame length')
        value.pop('codec', None)
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
                self._call('close')
        except Exception:
            pass
        finally:
            if self.connection is not None:
                self.connection.close()
            if self.process_id is not None:
                try:
                    self.context.worker.process_terminate(self.context.id, self.process_id)
                except Exception:
                    pass

    def __exit__(self, *args):
        self.close()
