# Async Python

Methods with an async counterpart expose it through `.aio`:

```python
import asyncio
from sandweave import Sandbox

async def evaluate(source):
    async with await Sandbox.create.aio(network="offline") as env:
        await env.files.write_text.aio("/workspace/main.py", source)
        result = await env.run.aio("python /workspace/main.py", timeout=5)
        return result.stdout

async def main():
    result = await evaluate("print(2 + 2)")
    print(result)

asyncio.run(main())
```

In a notebook with an active event loop, use `await main()` instead of
`asyncio.run(main())`.

## Cleanup and errors

An owned async context terminates its sandbox when it exits. Nonzero command
exits return results by default, just as in synchronous Python. Add `check=True`
to raise `CommandError`; timeouts and connection failures still raise.

Creation, file operations, commands, snapshots, and lifecycle methods use the
same arguments and ownership rules in both forms.

## Run a few tasks concurrently

```python
async def main():
    results = await asyncio.gather(
        evaluate("print(1 + 1)"),
        evaluate("print(2 + 2)"),
    )
    print(results)
```

For a larger workload, use a bounded [pool](pools.md) to control concurrency
and keep a reserve of ready environments.
