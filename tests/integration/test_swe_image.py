"""Run an official SWE-rebench task image and its repository's sequence tests."""
import json
import os
from pathlib import Path
import time

import pytest

from sandweave import Sandbox

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_SWE_IMAGE_INTEGRATION'), reason='explicit SWE image acceptance required')]

IMAGE = ('docker://swerebench/sweb.eval.x86_64.biopython_1776_biopython-5005'
         '@sha256:5c44c88cfe52bb11bdcd25535035194e2914873192dcd6e0a10d7e13893ca3ae')


def test_swe_rebench_biopython_image():
    output = Path(os.environ['SANDWEAVE_SWE_IMAGE_INTEGRATION'])
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with Sandbox(image=IMAGE, cpu=2, memory='4GiB', startup_timeout=1800) as env:
        information = {'image': env.info['image'], 'timings': env.timings,
                       'wall_seconds': time.monotonic() - started}
        revision = env.run('git rev-parse HEAD', cwd='/testbed', check=True)
        information['revision'] = revision.stdout.strip()
        result = env.run('source /opt/conda/etc/profile.d/conda.sh && conda activate testbed && '
                         'python -m pytest test_seq.py test_Seq_objs.py -q', shell='/bin/bash', cwd='/testbed/Tests', timeout=300)
        information.update(stdout=result.stdout, stderr=result.stderr, returncode=result.returncode)
        (output / 'result.json').write_text(json.dumps(information, indent=2))
        assert result.returncode == 0, result.stdout + result.stderr
