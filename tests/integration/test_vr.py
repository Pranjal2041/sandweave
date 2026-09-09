"""GPU acceptance: paired eyes, acknowledged input, recording and cold restoration."""
import os
from pathlib import Path
import time
import uuid

import pytest

from sandweave import Sandbox, Slurm, UnsupportedFeature
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.gpu,
              pytest.mark.skipif(not os.environ.get('SANDWEAVE_GPU_INTEGRATION'), reason='explicit allocated GPU required')]


@pytest.fixture(scope='module', autouse=True)
def release_idle_test_worker():
    yield
    connection = target().connection() if target() else local_connection()
    connection.call('_shutdown_if_idle')
    connection.close()


def target():
    job = os.environ.get('SANDWEAVE_TEST_JOB')
    return Slurm.connect(job, cpus=12) if job else None


@pytest.mark.parametrize('game', ['opensaber', 'gunspinning'])
def test_vr_both_eyes_controls_pause_record_and_restore(game):
    import numpy as np
    from PIL import Image
    directory = Path(os.environ['SANDWEAVE_ASSETS']) / 'runs/sdk-acceptance/vr' / (game + '-' + uuid.uuid4().hex[:8])
    existing = os.environ.get('SANDWEAVE_TEST_VR_EXISTING') if game == 'opensaber' else None
    instance = (Sandbox.connect(existing, target=target()) if existing else
                Sandbox(template='vr/' + game, gpu='L40S', target=target()))
    with instance as env:
        assert env.run('test ! -e /dev/kvm').returncode == 0
        observation = env.vr.observe()
        assert observation.left.shape == observation.right.shape == (1080, 960, 3)
        assert observation.left.dtype == np.uint8
        assert np.any(observation.left != observation.right)
        original = observation.left.copy()
        following = env.vr.step({'head': {'position': [.1, 1.6, 0], 'orientation': [0, 0, 0, 1]}})
        assert following.metadata['capture_begin_ns'] >= following.metadata['input']['guest_ack_ns']
        assert np.array_equal(original, observation.left), 'an owned observation changed underneath its caller'
        process_ids = env.run('pgrep -x monado-service').stdout
        env.pause(); env.resume()
        assert env.run('pgrep -x monado-service').stdout == process_ids
        env.vr.observe(after=following.metadata['sequence'])
        with pytest.raises(UnsupportedFeature):
            env.snapshot(state='memory')
        with env.vr.record(directory, fps=20) as recording:
            for i in range(50):
                env.vr.step({'head': {'position': [.08 if i % 2 else -.08, 1.6, 0]}})
                time.sleep(.08)
        assert recording.metadata['eye_count'] == 2
        for path in recording.metadata['videos'].values():
            assert Path(path).stat().st_size > 1000
        Image.fromarray(env.vr.observe().left).save(directory / 'final-left.png')
        Image.fromarray(env.vr.observe().right).save(directory / 'final-right.png')
        saved = env.stop()
        assert saved.state == 'filesystem'
    with Sandbox(cache=saved, target=target()) as restored:
        observation = restored.vr.observe()
        assert observation.left.shape == observation.right.shape
        Image.fromarray(observation.left).save(directory / 'restored-left.png')
        Image.fromarray(observation.right).save(directory / 'restored-right.png')
