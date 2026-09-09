"""One implementation for ordinary calls and Modal-style .aio calls."""
import asyncio
import functools


class dualmethod:
    def __init__(self, function):
        self.function = function
        self.async_function = None
        functools.update_wrapper(self, function)

    def async_impl(self, function):
        self.async_function = function
        return function

    def __get__(self, instance, owner):
        function = self.function.__get__(instance, owner)

        @functools.wraps(function)
        def call(*args, **kwargs):
            return function(*args, **kwargs)

        async def aio(*args, **kwargs):
            if self.async_function is not None:
                return await self.async_function.__get__(instance, owner)(*args, **kwargs)
            # Read/wait cancellation does not cancel the underlying process.
            return await asyncio.to_thread(function, *args, **kwargs)

        call.aio = aio
        return call


class dualclassmethod(dualmethod):
    def __get__(self, instance, owner):
        return super().__get__(owner, type(owner))
