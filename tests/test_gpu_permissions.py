import json
import os
import sys
from types import SimpleNamespace

import pytest

from sandweave.sandbox import workspace
from sandweave.sandbox.runtimes.gvisor import driver


@pytest.mark.parametrize('cached', [False, True])
def test_gpu_descriptors_are_readable_by_guest_desktop_users(tmp_path, monkeypatch, cached):
    def stage(destination):
        destination.mkdir(parents=True)
        (destination / 'lib').mkdir(mode=0o700)
        for name in ('driver.json', 'egl.json', 'vulkan.json'):
            path = destination / name
            path.write_text(json.dumps({'test': name}))
            path.chmod(0o600)

    destination = tmp_path / 'tools/gpu/driver'
    if cached:
        stage(destination)
    module = SimpleNamespace(
        eligible_devices=lambda: [0], allocated_device=lambda index: None,
        device_identity=lambda index: {'uuid': 'GPU-test'}, stage_driver=stage,
    )
    monkeypatch.setitem(sys.modules, 'gvisor_gpu', module)
    monkeypatch.setattr(driver.subprocess, 'check_output', lambda *a, **k: 'Test GPU')
    monkeypatch.setattr(workspace, 'assets', lambda: tmp_path / 'source')
    runtime = driver.Runtime.__new__(driver.Runtime)
    runtime.root = tmp_path
    previous = os.umask(0o077)
    try:
        assert runtime.gpu({}) == 0
    finally:
        os.umask(previous)
    for name in ('driver.json', 'egl.json', 'vulkan.json'):
        path = destination / name
        assert path.stat().st_mode & 0o777 == 0o644
        assert json.loads(path.read_text()) == {'test': name}
    assert destination.stat().st_mode & 0o777 == 0o755
    assert (destination / 'lib').stat().st_mode & 0o777 == 0o755
