"""Pull a prepared task from the 295 split and invoke its canonical verifier."""
from dataclasses import asdict
import json
import os
from pathlib import Path

import pytest

from sandweave import Benchmark
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_OSWORLD_DESKTOP_CACHE'),
    reason='explicit disposable worker with prepared OSWorld filesystem required')]


def test_unanimous_295_public_pull_screenshot_and_evaluation(tmp_path):
    output = Path(os.environ.get('SANDWEAVE_OSWORLD_OUTPUT', str(tmp_path)))
    output.mkdir(parents=True, exist_ok=True)
    bench = Benchmark('osworld-unanimous-295', capacity=1, preload=0,
                      template=None, cache=os.environ['SANDWEAVE_OSWORLD_DESKTOP_CACHE'])
    try:
        assert len(bench.tasks) == 295
        task = bench.next()
        try:
            assert task.id == bench.tasks[0].id
            image = task.env.desktop.screenshot()
            assert image.size == (1920, 1080)
            image.save(output / 'initial.png')
            readiness = json.loads(task.env.files.read_text('/var/log/sandweave-osworld-readiness.json'))
            (output / 'readiness.json').write_text(json.dumps(readiness, indent=2) + '\n')
            result = task.evaluate()
            assert result.task_id == task.id
            assert result.score in (0, 100)
            assert result.passed == (result.score == 100)
            (output / 'result.json').write_text(json.dumps(asdict(result), indent=2) + '\n')
            print(json.dumps(asdict(result)), flush=True)
        finally:
            task.close()
    finally:
        bench.close()
        connection = local_connection()
        connection.call('_shutdown_if_idle')
        connection.close()
