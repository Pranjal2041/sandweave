"""Run state transitions in threads and wait for worker replies asynchronously.

Lifecycle functions yield RPC requests only between transactions. Each advance
runs to completion on a control thread, so thread-local transactions never span
an await. The scheduled future covers the entire operation, including its final
commit; allocation serialization and shutdown draining use that same future.
"""
import asyncio
from dataclasses import dataclass
from functools import wraps


@dataclass
class Request:
    connection: object
    operation: str
    parameters: dict

    def call(self):
        return self.connection.call(self.operation, **self.parameters)

    async def acall(self):
        return await self.connection.acall(self.operation, **self.parameters)


def rpc(connection, operation, **parameters):
    return Request(connection, operation, parameters)


def advance(steps, value=None, error=None):
    try:
        return False, steps.throw(error) if error is not None else steps.send(value)
    except StopIteration as result:
        return True, result.value


def lifecycle(function):
    """Keep direct internal calls synchronous; the controller schedules .steps."""
    @wraps(function)
    def run(*args, **kwargs):
        steps = function(*args, **kwargs)
        value = error = None
        try:
            while True:
                done, request = advance(steps, value, error)
                if done:
                    return request
                value = error = None
                try:
                    value = request.call()
                except Exception as failure:
                    error = failure
        finally:
            steps.close()
    run.steps = function
    return run


async def execute(steps, executor):
    loop = asyncio.get_running_loop()
    value = error = None
    try:
        while True:
            done, request = await loop.run_in_executor(executor, advance, steps, value, error)
            if done:
                return request
            value = error = None
            try:
                value = await request.acall()
            except Exception as failure:
                error = failure
    finally:
        await loop.run_in_executor(executor, steps.close)
