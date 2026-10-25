"""A GNOME desktop on the KVM platform, compared with the default platform."""
import json
import os
from pathlib import Path
import time

import pytest

from sandweave import Memory, Sandbox
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit isolated worker required'),
    pytest.mark.skipif(not os.access('/dev/kvm', os.R_OK | os.W_OK), reason='this host does not provide /dev/kvm')]
MEMORY = Memory('4GiB', '1GiB')
IDLE_SECONDS = 30


def cpu_seconds(pid):
    """User and system CPU of a process and its descendants, from /proc."""
    ticks, pending, seen = 0, [pid], set()
    children = {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            except OSError:
                continue
            children.setdefault(int(fields[1]), []).append((int(entry.name), int(fields[11]) + int(fields[12])))
    own = {}
    for parent, items in children.items():
        for child, value in items:
            own[child] = value
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        ticks += own.get(current, 0)
        pending.extend(child for child, _ in children.get(current, ()))
    return ticks / os.sysconf('SC_CLK_TCK')


def sentry(env):
    connection = local_connection()
    try:
        return connection.call('describe', identity=env.id)['runtime_status']['sentry']['pid']
    finally:
        connection.close()


def test_gnome_desktop_boots_and_takes_input_on_kvm():
    results = {}
    for label, option in (('systrap', {}), ('kvm', {'kvm': True})):
        started = time.perf_counter()
        with Sandbox(template='gnome', memory=MEMORY, startup_timeout=900, **option) as env:
            ready = time.perf_counter() - started
            assert env.info['kvm'] is (label == 'kvm')
            image = env.desktop.screenshot()
            assert image.size == (1920, 1080)
            assert len(set(image.resize((64, 36)).getdata())) > 20, 'the desktop rendered a blank frame'
            observation = env.desktop.step({'mouse': {'move': [960, 540]}})
            assert observation.metadata['input_sequence'] >= 1
            jpeg = env.desktop.capture_image(format='jpeg')
            assert jpeg.mime_type == 'image/jpeg'
            # Firefox sets its own GS base; it crashed on KVM before runtime 2026.10.24.2.
            if env.run('command -v firefox').returncode == 0:
                firefox = env.run('cd /tmp && timeout 120 firefox --headless --screenshot /tmp/shot.png about:blank '
                                  '>/dev/null 2>&1; echo $?; test -s /tmp/shot.png && echo saved',
                                  timeout=180, user='ga')
                assert firefox.stdout.split() == ['0', 'saved'], firefox
                results.setdefault('firefox', []).append(label)
            pid = sentry(env)
            before = cpu_seconds(pid)
            time.sleep(IDLE_SECONDS)
            idle = (cpu_seconds(pid) - before) / IDLE_SECONDS
            shots = os.environ.get('SANDWEAVE_KVM_SCREENSHOTS')
            if shots:
                Path(shots).mkdir(parents=True, exist_ok=True)
                env.desktop.screenshot().save(Path(shots) / f'gnome-{label}.png')
            results[label] = {**results.get(label, {}), 'ready_seconds': ready, 'idle_cpu_cores': idle}
    path = os.environ.get('SANDWEAVE_KVM_EVIDENCE')
    if path:
        evidence = json.loads(Path(path).read_text()) if Path(path).exists() else {}
        Path(path).write_text(json.dumps({**evidence, 'gnome': results}, indent=2, sort_keys=True) + '\n')
