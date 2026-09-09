"""Guest process handles with separate execution and waiting deadlines."""
from dataclasses import dataclass, field
import codecs
import io
import time

from .asyncio import dualmethod
from .errors import CommandError, CommandTimeout, OutputLimitExceeded

INLINE_LIMIT = 1024**2


@dataclass(frozen=True)
class CommandResult:
    stdout: str | bytes
    stderr: str | bytes
    returncode: int
    timings: dict = field(default_factory=dict)
    truncated: dict = field(default_factory=dict)
    output_refs: dict = field(default_factory=dict)
    output_limited: bool = False


class OutputStream:
    def __init__(self, process, name, binary=False):
        self.process, self.name, self.binary = process, name, binary
        self.offset = 0
        self.buffer = b'' if binary else ''
        self.decoder = None if binary else codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.raw_received = 0

    def _chunk(self):
        data = self.process.sandbox._call('process_output', process_id=self.process.id,
                                          stream=self.name, offset=self.offset, size=64*1024)
        self.offset += len(data)
        self.raw_received = len(data)
        return data if self.binary else self.decoder.decode(data)

    @dualmethod
    def read(self, size=-1):
        if size == 0:
            return b'' if self.binary else ''
        while size < 0 or len(self.buffer) < size:
            chunk = self._chunk()
            self.buffer += chunk
            if not self.raw_received:
                if self.process.poll() is not None:
                    self.buffer += self._chunk()
                    if self.raw_received:
                        continue
                    if not self.binary:
                        self.buffer += self.decoder.decode(b'', final=True)
                    break
                time.sleep(.01)
        result = self.buffer if size < 0 else self.buffer[:size]
        self.buffer = self.buffer[len(result):]
        return result

    @dualmethod
    def readline(self):
        newline = b'\n' if self.binary else '\n'
        while newline not in self.buffer:
            chunk = self._chunk()
            self.buffer += chunk
            if not self.raw_received:
                if self.process.poll() is not None:
                    self.buffer += self._chunk()
                    if self.raw_received:
                        continue
                    if not self.binary:
                        self.buffer += self.decoder.decode(b'', final=True)
                    result, self.buffer = self.buffer, self.buffer[:0]
                    return result
                time.sleep(.01)
        index = self.buffer.index(newline) + 1
        result, self.buffer = self.buffer[:index], self.buffer[index:]
        return result

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    async def __aiter__(self):
        while line := await self.readline.aio():
            yield line


class InputStream:
    def __init__(self, process):
        self.process, self.closed = process, False

    @dualmethod
    def write(self, data):
        if self.closed:
            raise ValueError('stdin is closed')
        data = data.encode() if isinstance(data, str) else bytes(data)
        written = 0
        while written < len(data):
            count = self.process.sandbox._call('process_stdin', process_id=self.process.id,
                                               data=data[written:written+1024**2])
            written += count
            if count == 0:
                time.sleep(.01)
        return written

    @dualmethod
    def close(self):
        if not self.closed:
            self.process.sandbox._call('process_stdin', process_id=self.process.id, close=True)
            self.closed = True

    def flush(self):
        pass


class Process:
    def __init__(self, sandbox, identity, *, binary=False):
        self.sandbox, self.id, self.binary = sandbox, identity, binary
        self.stdout, self.stderr = OutputStream(self, 'stdout', binary), OutputStream(self, 'stderr', binary)
        self.stdin = InputStream(self)

    @dualmethod
    def poll(self):
        return self.sandbox._call('process_status', process_id=self.id)['returncode']

    def result(self, limit=INLINE_LIMIT):
        state = self.sandbox._call('process_status', process_id=self.id)
        if state['returncode'] is None:
            raise RuntimeError('process has not completed')
        outputs = {}
        for stream in ('stdout', 'stderr'):
            data = self.sandbox._call('process_output', process_id=self.id, stream=stream, offset=0, size=limit)
            outputs[stream] = data if self.binary else data.decode(errors='replace')
        return CommandResult(**outputs, returncode=state['returncode'],
                             output_limited=state.get('output_limited', False),
                             timings={'guest_seconds': (state['finished_ns'] - state['started_ns'])/1e9},
                             truncated={s: state[s+'_size'] > limit for s in outputs},
                             output_refs={s: {'sandbox': self.sandbox.id, 'process': self.id, 'stream': s}
                                          for s in outputs if state[s+'_size'] > limit})

    @dualmethod
    def wait(self, timeout=None, *, check=False):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            state = self.sandbox._call('process_status', process_id=self.id)
            if state['returncode'] is not None:
                if state.get('output_limited'):
                    raise OutputLimitExceeded('combined command output exceeded its spool budget',
                                              result=self.result(), operation_id=self.id)
                if state.get('timed_out'):
                    raise CommandTimeout('guest execution deadline exceeded', result=self.result(), operation_id=self.id)
                if check and state['returncode']:
                    raise CommandError(f'command exited with {state["returncode"]}', result=self.result(), operation_id=self.id)
                return state['returncode']
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('waiting deadline exceeded; the command is still running')
            time.sleep(.01)

    @dualmethod
    def terminate(self):
        self.sandbox._call('process_terminate', process_id=self.id)
        return self.wait(timeout=10)

    @dualmethod
    def resize(self, rows, cols):
        return self.sandbox._call('process_resize', process_id=self.id, rows=rows, cols=cols)
