import json

from sandweave.sandbox.runtimes.gvisor.engine import launch_runtime


def test_guest_tools_follow_the_runtime_each_launch_uses(tmp_path):
    root = tmp_path / 'worker'
    (root / 'tools/gvisor-socket').mkdir(parents=True)
    (root / 'tools/gvisor-socket/runtime.json').write_text(json.dumps({'path': 'tools/runtime-builds/default'}))
    snapshot = tmp_path / 'snapshot'
    snapshot.mkdir()
    # A fresh launch uses the default runtime when it already has the features.
    assert launch_runtime(root, []) == 'tools/runtime-builds/default'
    assert launch_runtime(root, ['--runtime-build', 'tools/runtime-builds/feature']) == 'tools/runtime-builds/feature'
    # A restore keeps the runtime recorded with its launch settings.
    assert launch_runtime(root, [], snapshot) == 'tools/runtime-builds/default'
    (snapshot / 'launch-settings.json').write_text(json.dumps({'runtime': {'path': 'tools/runtime-builds/saved'}}))
    assert launch_runtime(root, [], snapshot) == 'tools/runtime-builds/saved'
    assert launch_runtime(root, ['--runtime-build', 'tools/runtime-builds/feature'], snapshot) == 'tools/runtime-builds/feature'
