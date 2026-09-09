"""Allocated-device compute, pause, cold restore and explicit CUDA-only checkpoint."""
import json
import os
from pathlib import Path

import pytest

from sandweave import Sandbox, GPU, UnsupportedFeature
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.gpu,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_GPU_INTEGRATION'), reason='explicit allocated GPU required')]


@pytest.fixture(scope='module', autouse=True)
def release_idle_test_worker():
    yield
    connection = local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()


def probe(env):
    return json.loads(env.run("python -c 'import urllib.request; print(urllib.request.urlopen(\"http://127.0.0.1:8000\").read().decode())'").stdout)


@pytest.mark.parametrize('runtime', ['gvisor', 'apptainer'])
def test_cuda_compute_pause_and_cold_restore(runtime):
    with Sandbox(template='cuda', runtime=runtime, gpu='L40S') as env:
        assert env.run('test ! -e /dev/kvm').returncode == 0
        source = Path(os.environ['SANDWEAVE_ASSETS']) / 'scripts/gpu-checkpoint-cuda-probe.py'
        env.files.upload(source, '/workspace/probe.py')
        process = env.exec('python -u /workspace/probe.py')
        assert json.loads(process.stdout.readline())['ready'] is True
        assert json.loads(process.stdout.readline())['port'] == 8000
        before = probe(env)
        env.pause(); env.resume()
        after = probe(env)
        assert after['nonce'] == before['nonce'] and after['value'] == before['value'] == 73
        env.files.write_text('/workspace/persist', before['nonce'])
        saved = env.stop(state='filesystem')
    with Sandbox(cache=saved) as restored:
        assert restored.files.read_text('/workspace/persist') == before['nonce']
        process = restored.exec('python -u /workspace/probe.py')
        assert json.loads(process.stdout.readline())['nonce'] != before['nonce']
        assert json.loads(process.stdout.readline())['port'] == 8000
        assert probe(restored)['value'] == 73


def test_experimental_cuda_memory_checkpoint():
    with Sandbox(template='cuda', gpu='L40S') as env:
        source = Path(os.environ['SANDWEAVE_ASSETS']) / 'scripts/gpu-checkpoint-cuda-probe.py'
        env.files.upload(source, '/workspace/probe.py')
        process = env.exec('python -u /workspace/probe.py')
        assert json.loads(process.stdout.readline())['ready'] is True
        process.stdout.readline()
        before = probe(env)
        with pytest.raises(UnsupportedFeature):
            env.snapshot(state='memory')
        saved = env.snapshot(state='memory', experimental_gpu_live=True)
        with pytest.raises((ValueError, UnsupportedFeature)):
            Sandbox(snapshot=saved)
        with Sandbox(snapshot=saved, experimental_gpu_live=True) as restored:
            after = probe(restored)
            for key in ('nonce', 'pid', 'address', 'value'):
                assert after[key] == before[key]


def test_mps_partition_kernel_memory_cap_and_peer_survival():
    source = Path(os.environ['SANDWEAVE_ASSETS']) / 'scripts/gpu-mps-client-probe.py'
    with Sandbox(template='cuda', gpu=GPU(model='L40S', sm_chunks=1, client_memory='1GiB', experimental=True)) as first:
        with Sandbox(template='cuda', gpu=GPU(model='L40S', sm_chunks=2, experimental=True)) as peer:
            first.files.upload(source, '/workspace/mps.py')
            peer.files.upload(source, '/workspace/mps.py')
            result = first.run('python /workspace/mps.py --expect-sms 4 --memory-limit-mib 1024', timeout=60)
            assert '1234567' in result.stdout
            assert '1234567' in peer.run('python /workspace/mps.py --expect-sms 8', timeout=60).stdout
            first.terminate()
            assert '1234567' in peer.run('python /workspace/mps.py --expect-sms 8', timeout=60).stdout
