"""Pool recording through outbound worker relays, including interrupted capture."""
import json
import os
from pathlib import Path
import signal
import time

import pytest

from sandweave import Pool, Memory, Recording
from sandweave.sandbox.ownership import process_alive
from test_weave_live import cluster, wait_for
from test_desktop_recording_live import decode_video

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_WEAVE_RECORDING_INTEGRATION'), reason='explicit disposable desktop cluster required')]


def test_recordings_survive_pool_file_reclamation_and_recorder_crash(cluster, tmp_path):
    with Pool(target='weave-live', template='gnome', size=2, warm=1,
              memory=Memory(guest='2GiB', runtime='512MiB'), recording=Recording(fps=5),
              retain_baseline=False, startup_timeout=300, wait_timeout=600) as pool:
        for connection in cluster.test_workers:
            for record in connection.call('list'):
                assert not record.get('recording', {}).get('segments')
        with pool.acquire() as env:
            assert env.recording.info['state'] == 'recording'
            time.sleep(2)
            env_id = env.id
            report = env.recording.info
            # This is the recorder of our newly created disposable sandbox.
            # Kill only that exact process, never a worker or another desktop.
            segment = report['segments'][-1]
            path = Path(report['location'])/segment['id']/'segment.json'
            record = json.loads(path.read_text())
            assert Path(report['location']).name == env_id
            assert process_alive(record['process']) is True
            from sandweave.sandbox.runtimes.router import Runtime
            workspace = Path(report['location']).parent.parent
            Runtime(workspace).adapter('gvisor').manager._signal(
                record['process']['pid'], int(record['process']['started']), signal.SIGKILL)
            wait_for(lambda: env.recording.info['state'] == 'partial')
    assert pool.info['artifacts_released']
    downloaded = env.recording.download(Path(os.environ.get('SANDWEAVE_TEST_ARTIFACTS', str(tmp_path)))/'retained-recording')
    saved = json.loads((downloaded/'recording.json').read_text())
    assert saved['state'] == 'partial'
    frames = decode_video(downloaded/saved['segments'][0]['id']/'video.mp4')
    assert frames
    from PIL import Image
    Image.fromarray(frames[-1]).save(downloaded/'decoded-partial.png')
    env.recording.delete()
    assert env.recording.info['segments'] == []
