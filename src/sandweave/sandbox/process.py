"""Guest process handles with separate execution and waiting deadlines."""
from dataclasses import dataclass, field
import asyncio
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
        data = b'' if self._finished() else self.process.sandbox._call('process_output', process_id=self.process.id,
                                          stream=self.name, offset=self.offset, size=1024**2)
        return self._decode(data)

    def _finished(self):
        final = getattr(self.process, '_final', None)
        return final is not None and self.offset >= final[self.name + '_size']

    def _decode(self, data):
        self.offset += len(data)
        self.raw_received = len(data)
        return data if self.binary else self.decoder.decode(data)

    async def _achunk(self):
        data = b'' if self._finished() else await self.process._acall('process_output',
            stream=self.name, offset=self.offset, size=1024**2)
        return self._decode(data)

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

    @read.async_impl
    async def _read_async(self, size=-1):
        if size == 0:
            return b'' if self.binary else ''
        while size < 0 or len(self.buffer) < size:
            self.buffer += await self._achunk()
            if not self.raw_received:
                if await self.process.poll.aio() is not None:
                    self.buffer += await self._achunk()
                    if self.raw_received:
                        continue
                    if not self.binary:
                        self.buffer += self.decoder.decode(b'', final=True)
                    break
                await asyncio.sleep(.01)
        result = self.buffer if size < 0 else self.buffer[:size]
        self.buffer = self.buffer[len(result):]
        return result

    @readline.async_impl
    async def _readline_async(self):
        newline = b'\n' if self.binary else '\n'
        while newline not in self.buffer:
            self.buffer += await self._achunk()
            if not self.raw_received:
                if await self.process.poll.aio() is not None:
                    self.buffer += await self._achunk()
                    if self.raw_received:
                        continue
                    if not self.binary:
                        self.buffer += self.decoder.decode(b'', final=True)
                    result, self.buffer = self.buffer, self.buffer[:0]
                    return result
                await asyncio.sleep(.01)
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

    @write.async_impl
    async def _write_async(self, data):
        if self.closed:
            raise ValueError('stdin is closed')
        data = data.encode() if isinstance(data, str) else bytes(data)
        written = 0
        while written < len(data):
            count = await self.process._acall('process_stdin', data=data[written:written+1024**2])
            written += count
            if count == 0:
                await asyncio.sleep(.01)
        return written

    @close.async_impl
    async def _close_async(self):
        if not self.closed:
            await self.process._acall('process_stdin', close=True)
            self.closed = True

    def flush(self):
        pass


class Process:
    def __init__(self, sandbox, identity, *, binary=False):
        self.sandbox, self.id, self.binary = sandbox, identity, binary
        self.stdout, self.stderr = OutputStream(self, 'stdout', binary), OutputStream(self, 'stderr', binary)
        self.stdin = InputStream(self)
        self._final = None

    async def _acall(self, operation, **params):
        if hasattr(self.sandbox, '_acall'):
            return await self.sandbox._acall(operation, process_id=self.id, **params)
        return await asyncio.to_thread(self.sandbox._call, operation, process_id=self.id, **params)

    def _remember(self, state):
        if state['returncode'] is not None:
            self._final = state
        return state

    @dualmethod
    def poll(self):
        return self._remember(self._final or self.sandbox._call('process_status', process_id=self.id))['returncode']

    @poll.async_impl
    async def _poll_async(self):
        return self._remember(self._final or await self._acall('process_status'))['returncode']

    @dualmethod
    def result(self, limit=INLINE_LIMIT):
        if type(limit) is not int or not 0 <= limit <= 4*1024**2:
            raise ValueError('invalid output request')
        state = self._remember(self._final or self.sandbox._call('process_status', process_id=self.id))
        if state['returncode'] is None:
            raise RuntimeError('process has not completed')
        outputs = {}
        for stream in ('stdout', 'stderr'):
            data = self.sandbox._call('process_output', process_id=self.id, stream=stream, offset=0, size=limit) if limit and state[stream+'_size'] else b''
            outputs[stream] = data if self.binary else data.decode(errors='replace')
        return self._result(state, outputs, limit)

    @result.async_impl
    async def _result_async(self, limit=INLINE_LIMIT):
        if type(limit) is not int or not 0 <= limit <= 4*1024**2:
            raise ValueError('invalid output request')
        state = self._remember(self._final or await self._acall('process_status'))
        if state['returncode'] is None:
            raise RuntimeError('process has not completed')
        outputs = {}
        for stream in ('stdout', 'stderr'):
            data = await self._acall('process_output', stream=stream, offset=0, size=limit) if limit and state[stream+'_size'] else b''
            outputs[stream] = data if self.binary else data.decode(errors='replace')
        return self._result(state, outputs, limit)

    def _result(self, state, outputs, limit):
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
            state = self._remember(self._final or self.sandbox._call('process_status', process_id=self.id))
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

    @wait.async_impl
    async def _wait_async(self, timeout=None, *, check=False):
        deadline = None if timeout is None else time.monotonic() + timeout
        delay = .01
        while True:
            state = self._remember(self._final or await self._acall('process_status'))
            if state['returncode'] is not None:
                if state.get('output_limited'):
                    raise OutputLimitExceeded('combined command output exceeded its spool budget',
                        result=await self.result.aio(), operation_id=self.id)
                if state.get('timed_out'):
                    raise CommandTimeout('guest execution deadline exceeded', result=await self.result.aio(), operation_id=self.id)
                if check and state['returncode']:
                    raise CommandError(f'command exited with {state["returncode"]}', result=await self.result.aio(), operation_id=self.id)
                return state['returncode']
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('waiting deadline exceeded; the command is still running')
            await asyncio.sleep(delay)
            delay = min(.05, delay * 1.5)

    @dualmethod
    def terminate(self):
        self.sandbox._call('process_terminate', process_id=self.id)
        return self.wait(timeout=10)

    @terminate.async_impl
    async def _terminate_async(self):
        await self._acall('process_terminate')
        return await self.wait.aio(timeout=10)

    @dualmethod
    def resize(self, rows, cols):
        return self.sandbox._call('process_resize', process_id=self.id, rows=rows, cols=cols)
