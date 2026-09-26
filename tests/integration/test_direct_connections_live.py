"""Exercise direct pool leases against two real workers, optionally older SDKs."""
import asyncio

from sandweave import Memory, Pool, Sandbox
from test_weave_live import cluster, pytestmark


def test_direct_pool_data_and_reconnect(cluster, monkeypatch):
    for worker in cluster.workers:
        cluster.connection.call('worker_update', identity=worker['id'], slots=4)
    pool = Pool(target=cluster, connection='direct', size=8, warm=8,
                memory=Memory('256MiB', '256MiB'), wait_timeout=600)
    def forbid(*args, **kwargs):
        raise AssertionError('sandbox data RPC reached the controller')
    async def run():
        await pool.start.aio()
        leases = [pool.acquire() for _ in range(8)]
        environments = await asyncio.gather(*(lease.__aenter__() for lease in leases))
        try:
            async def episode(index, env):
                with monkeypatch.context() as patch:
                    patch.setattr(env._connection.control, 'call', forbid)
                    patch.setattr(env._connection.control, 'acall', forbid)
                    path = '/tmp/direct-data'
                    await env.files.write_text.aio(path, str(index))
                    assert await env.files.read_text.aio(path) == str(index)
                    assert (await env.run.aio('printf direct', check=True)).stdout == 'direct'
                    # Force status/output traffic while the process is running.
                    process = await env.exec.aio('printf first; sleep .1; printf last')
                    await process.wait.aio()
                    result = await process.result.aio()
                    assert result.stdout == 'firstlast' and result.returncode == 0
                # Borrowing and closing a handle must not release this lease.
                borrowed = await Sandbox.connect.aio(env.id, target=cluster, connection='direct')
                try:
                    assert (await borrowed.run.aio('printf reconnected', check=True)).stdout == 'reconnected'
                finally:
                    await borrowed.close.aio()
                assert (await env.run.aio('true')).returncode == 0
            await asyncio.gather(*(episode(i, env) for i, env in enumerate(environments)))
        finally:
            await asyncio.gather(*(lease.__aexit__(None, None, None) for lease in leases))
    try:
        asyncio.run(run())
    finally:
        pool.close()
    assert all(record['released'] for record in cluster.info['sandboxes'])
