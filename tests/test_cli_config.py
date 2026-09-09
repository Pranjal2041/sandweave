import json

from sandweave.cli import main


def test_cli_does_not_replace_invalid_explicit_cpu_values():
    import argparse
    import pytest
    from sandweave.cli import creation, creation_options
    parser = argparse.ArgumentParser()
    creation_options(parser)
    for arguments in (['--cpu', '0', '--cpu-weight', '100'], ['--cpu-weight', '0']):
        with pytest.raises(ValueError):
            creation(parser.parse_args(arguments))


def test_configure_preserves_named_targets(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'home'))
    assert main(['targets', 'add', 'training', '--config', '{"job_id":"12345"}']) == 0
    assert main(['configure', '--assets', str(tmp_path)]) == 0
    config = json.loads((tmp_path / 'home/config.json').read_text())
    assert config['targets']['training'] == {'job_id': '12345'}
    assert config['assets'] == str(tmp_path)
    assert main(['targets', 'list']) == 0
    assert json.loads(capsys.readouterr().out) == {'training': {'job_id': '12345'}}


def test_async_allocation_cancel_reconciles_owned_job(monkeypatch, tmp_path):
    import asyncio
    import threading
    from types import SimpleNamespace
    from sandweave import Slurm
    import sandweave.sandbox.targets as targets
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    started = threading.Event()
    cancelled = []
    def run(argv, **kwargs):
        if argv[0] == 'sbatch':
            return SimpleNamespace(stdout='12345\n')
        assert argv == ['scancel', '12345']
        cancelled.append('12345')
        return SimpleNamespace(returncode=0)
    def wait(self, timeout=None, cancel_event=None):
        started.set()
        assert cancel_event.wait(5)
        raise InterruptedError('cancelled')
    monkeypatch.setattr(targets.subprocess, 'run', run)
    monkeypatch.setattr(Slurm, '_wait', wait)
    async def exercise():
        task = asyncio.create_task(Slurm.acquire.aio())
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError('allocation cancellation was lost')
    asyncio.run(exercise())
    assert cancelled == ['12345']
