"""Isolated local-candidate test. Does not attach to existing sandboxes."""
import io
import json
import os
from pathlib import Path
import statistics
import time

ROOT = Path(os.environ.get('WCB_CAPTURE_ROOT', '/tmp/sw-fast-capture-20260920'))
os.environ['SANDWEAVE_HOME'] = str(ROOT / 'home')
os.environ['SANDWEAVE_ASSETS'] = str(ROOT / 'assets')
os.environ['SANDWEAVE_GPU_LIMIT'] = ''


def main():
    from PIL import Image, ImageChops
    from sandweave import Sandbox, CPU, Memory
    from sandweave.sandbox.targets import local_connection
    from sandweave.installation import record_installation
    # This is a new private copy with a freshly compiled bridge, not an edit
    # to the official installation used by the evaluation workers.
    record_installation(ROOT / 'assets')
    home = ROOT / 'home'
    home.mkdir(exist_ok=True)
    (home / 'config.json').write_text(json.dumps({'assets': str(ROOT / 'assets')}))
    (home / 'bin').mkdir(exist_ok=True)
    apptainer = home / 'bin/apptainer'
    if not apptainer.exists():
        apptainer.symlink_to('/tmp/wcb-weave-20260912/sandweave/bin/apptainer')
    out = ROOT / 'results'
    out.mkdir(exist_ok=True)
    rows = []
    try:
        with Sandbox(template='gnome', cpu=CPU(vcpus=4, weight=100),
                     memory=Memory('8GiB', '1GiB'), gpu=False, startup_timeout=300) as env:
            print('OWN DIAGNOSTIC SANDBOX', env.id, flush=True)
            (out / 'sandbox.json').write_text(json.dumps({'id': env.id, 'worker': env.info['worker']}))
            env.desktop.mouse.move(960, 540)
            for _ in range(3):
                env.desktop.capture_png()
            for index in range(30):
                for method in ('screenshot', 'capture_png'):
                    begin = time.perf_counter_ns()
                    value = getattr(env.desktop, method)()
                    received = time.perf_counter_ns()
                    path = out / f'{method}-{index:02d}.png'
                    if method == 'screenshot':
                        value.save(path, 'PNG')
                        metadata = {}
                    else:
                        path.write_bytes(value.png)
                        metadata = value.metadata
                        assert metadata['cursor'] is True
                        assert 0 < metadata['capture_started_wall_ns'] <= metadata['capture_completed_wall_ns'] <= time.time_ns()
                    end = time.perf_counter_ns()
                    rows.append({'index': index, 'method': method, 'request_ms': (received-begin)/1e6,
                                 'write_ms': (end-received)/1e6, 'total_ms': (end-begin)/1e6,
                                 'bytes': path.stat().st_size, 'metadata': metadata})
            for xy in ((960,540), (0,0), (1919,1079)):
                env.desktop.mouse.move(*xy)
                plain = env.desktop.capture_png(cursor=False)
                cursor = env.desktop.capture_png(cursor=True)
                a, b = (Image.open(io.BytesIO(x.png)) for x in (plain, cursor))
                assert a.size == b.size == (1920,1080)
                bounds = ImageChops.difference(a,b).getbbox()
                assert bounds is not None, f'cursor absent at {xy}'
                (out / f'cursor-{xy[0]}-{xy[1]}.png').write_bytes(cursor.png)
                print('CURSOR DIFFERENCE', xy, bounds, flush=True)
            # Fresh capture after pause/resume must work with a new helper generation.
            previous = cursor.metadata['generation']
            env.pause(); env.resume()
            resumed = env.desktop.capture_png()
            assert resumed.metadata['generation'] != previous
            (out / 'resumed.png').write_bytes(resumed.png)
    finally:
        (out / 'measurements.json').write_text(json.dumps(rows, indent=2))
        connection = local_connection()
        connection.call('_shutdown_if_idle')
        connection.close()
    for method in ('screenshot','capture_png'):
        selected = [r for r in rows if r['method'] == method]
        print(method, {key: round(statistics.mean(r[key] for r in selected),3)
                       for key in ('request_ms','write_ms','total_ms','bytes')}, flush=True)
    print('COMPLETE', flush=True)


if __name__ == '__main__':
    main()
