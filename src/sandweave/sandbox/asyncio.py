"""One implementation for ordinary calls and Modal-style .aio calls."""
import asyncio
import functools


class dualmethod:
    def __init__(self, function):
        self.function = function
        functools.update_wrapper(self, function)

    def __get__(self, instance, owner):
        function = self.function.__get__(instance, owner)

        @functools.wraps(function)
        def call(*args, **kwargs):
            return function(*args, **kwargs)

        async def aio(*args, **kwargs):
            # A running remote operation is not magically cancelled by cancelling
            # its awaiting Python task. Stateful callers provide reconciliation.
            return await asyncio.to_thread(function, *args, **kwargs)

        call.aio = aio
        return call


class dualclassmethod(dualmethod):
    def __get__(self, instance, owner):
        return super().__get__(owner, type(owner))
