"""Real Xvnc capture, independent animation, finalization and retained export."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from sandweave import Sandbox, Recording, Pool, Memory
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_RECORDING_INTEGRATION'), reason='explicit disposable desktop worker required')]


ANIMATION = '''import ctypes as c
import time
x = c.CDLL('libX11.so.6')
display, xid, uint, integer = c.c_void_p, c.c_ulong, c.c_uint, c.c_int
def bind(name, result, *args):
    f = getattr(x, name)
    f.restype, f.argtypes = result, list(args)
    return f
d = bind('XOpenDisplay', display, c.c_char_p)(b':1')
assert d
root = bind('XDefaultRootWindow', xid, display)(d)
w = bind('XCreateSimpleWindow', xid, display, xid, integer, integer, uint, uint, uint, xid, xid)(d, root, 100, 100, 640, 360, 0, 0, 0x142033)
bind('XStoreName', integer, display, xid, c.c_char_p)(d, w, b'Recording acceptance')
bind('XMapWindow', integer, display, xid)(d, w)
gc = bind('XCreateGC', display, display, xid, xid, display)(d, w, 0, None)
foreground = bind('XSetForeground', integer, display, display, xid)
fill = bind('XFillRectangle', integer, display, xid, display, integer, integer, uint, uint)
flush = bind('XFlush', integer, display)
counter = 0
while True:
    foreground(d, gc, 0x142033)
    fill(d, w, gc, 0, 0, 640, 360)
    foreground(d, gc, 0x00ff00)
    fill(d, w, gc, 30 + (counter*13)%480, 100, 90, 90)
    flush(d)
    counter += 1
    time.sleep(.07)
'''


@pytest.fixture(scope='module', autouse=True)
def idle_worker_cleanup():
    yield
    connection = local_connection()
    try:
        connection.call('_shutdown_if_idle')
    finally:
        connection.close()


def decode_video(path):
    import imageio_ffmpeg
    import numpy as np
    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt='rgb24')
    try:
        metadata = next(reader)
        width, height = metadata['size']
        return [np.frombuffer(data, dtype='uint8').reshape(height, width, 3).copy() for data in reader]
    finally:
        reader.close()


def test_recording_motion_pause_and_export_after_termination(tmp_path):
    artifact = Path(os.environ.get('SANDWEAVE_TEST_ARTIFACTS', str(tmp_path)))
    artifact.mkdir(parents=True, exist_ok=True)
    with Sandbox(template='gnome', recording=Recording(fps=10), ttl=240,
                 memory=Memory(guest='4GiB', runtime='1GiB')) as env:
        assert env.recording.info['state'] == 'recording'
        env.files.write_text('/workspace/animate.py', ANIMATION)
        env.exec('python /workspace/animate.py')
        env.run("xdotool search --sync --name 'Recording acceptance' windowactivate --sync", timeout=15)
        env.desktop.mouse.move(1500, 900)
        before = env.recording.info['frames_captured']
        # Deliberately close the client: there are no action/screenshot RPCs
        # during this interval, while the application continues repainting.
        env.close()
        time.sleep(3)
        with Sandbox.connect(env.id) as attached:
            assert attached.recording.info['frames_captured'] > before+5
            attached.desktop.screenshot().save(artifact/'desktop.png')
            # Compare the real RFB pointer with the cursor-free framebuffer.
            from sandweave.templates.gnome.rfb import Capture
            from PIL import ImageChops
            vnc = attached.info['vnc']
            visible = Capture(vnc['port'], vnc['password'].encode(), cursor=True)
            try:
                with_cursor = visible.frame()[0]
                without_cursor = attached.desktop.screenshot()
                region = (1450, 850, 1600, 1000)
                assert ImageChops.difference(with_cursor.crop(region), without_cursor.crop(region)).getbbox()
                with_cursor.crop(region).save(artifact/'cursor-visible.png')
                without_cursor.crop(region).save(artifact/'cursor-hidden.png')
            finally:
                visible.close()
            attached.pause()
            paused = attached.recording.info
            assert paused['state'] == 'paused'
            assert paused['segments'][-1]['state'] == 'complete'
            attached.resume()
            time.sleep(2)
            assert len(attached.recording.info['segments']) == 2
    # The owned context above terminated its sandbox, even though its client
    # handle was closed. Recording RPCs reconnect to the original worker.
    output = env.recording.download(artifact/'recorded')
    manifest = json.loads((output/'recording.json').read_text())
    assert manifest['state'] == 'stopped'
    assert len(manifest['segments']) == 2
    assert all(s['state'] == 'complete' for s in manifest['segments']), manifest
    video_frames = []
    for segment in manifest['segments']:
        frames = decode_video(output/segment['id']/'video.mp4')
        assert len(frames) == segment['frames_encoded']
        assert frames[0].shape == (1080, 1920, 3)
        timeline = [json.loads(line) for line in (output/segment['id']/'timeline.jsonl').read_text().splitlines()]
        assert all(row['capture_completed_ns'] >= row['capture_started_ns'] for row in timeline)
        assert all(b['capture_started_ns'] > a['capture_started_ns'] for a, b in zip(timeline, timeline[1:]))
        video_frames.extend(frames)
    assert len(video_frames) > 15
    import numpy as np
    positions = set()
    for frame in video_frames:
        patch = frame[100:500, 100:800]
        green = (patch[:,:,1] > 180) & (patch[:,:,0] < 50) & (patch[:,:,2] < 50)
        if green.sum() > 1000:
            positions.add(int(np.where(green)[1].mean()))
    assert len(positions) > 5, 'independent application animation must actually appear in the video'
    from PIL import Image
    for index in (0, len(video_frames)//2, len(video_frames)-1):
        Image.fromarray(video_frames[index]).save(artifact/f'decoded-{index}.png')
    env.recording.delete()
    assert env.recording.info['segments'] == []


def test_pool_only_records_leased_sandbox_and_keeps_export(tmp_path):
    with Pool(template='gnome', size=1, warm=1, recording=True,
              memory=Memory(guest='4GiB', runtime='1GiB'), ttl=240) as pool:
        idle = list(pool.idle)
        assert idle and idle[0].recording.info['segments'] == []
        with pool.acquire() as env:
            assert env.recording.info['state'] == 'recording'
            time.sleep(1)
    path = Path(os.environ.get('SANDWEAVE_TEST_ARTIFACTS', str(tmp_path)))/'pool-recording'
    result = subprocess.run([sys.executable, '-m', 'sandweave.cli', 'recording', 'download',
                             env.id, '--output', str(path)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads((path/'recording.json').read_text())
    assert report['segments'][0]['frames_encoded'] >= 1
    assert decode_video(path/report['segments'][0]['id']/'video.mp4')


def test_cursor_free_snapshot_and_manual_stop(tmp_path):
    with Sandbox(template='gnome', recording=Recording(fps=5, cursor=False), ttl=240,
                 memory=Memory(guest='4GiB', runtime='1GiB')) as env:
        env.desktop.mouse.move(1500, 900)
        time.sleep(1)
        snapshot = env.snapshot()
        assert len(env.recording.info['segments']) == 2
        time.sleep(1)
        env.recording.stop()
        env.pause()
        env.resume()
        assert len(env.recording.info['segments']) == 2
        assert env.recording.info['state'] == 'stopped'
    output = env.recording.download(Path(os.environ.get('SANDWEAVE_TEST_ARTIFACTS', str(tmp_path)))/'cursor-free')
    manifest = json.loads((output/'recording.json').read_text())
    assert all(not segment['cursor'] and segment['state'] == 'complete' for segment in manifest['segments'])
    for segment in manifest['segments']:
        frames = decode_video(output/segment['id']/'video.mp4')
        assert frames
        from PIL import Image
        Image.fromarray(frames[-1]).save(output/(segment['id']+'.png'))
    with Sandbox(snapshot=snapshot, ttl=240) as restored:
        assert restored.recording.info['state'] == 'off'
