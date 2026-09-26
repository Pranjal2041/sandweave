"""Bulk ingress through a routed namespace must not accumulate unbounded packets."""
import asyncio
import gzip
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from sandweave import CPU, Memory, Sandbox
from sandweave.sandbox.errors import UnsupportedFeature
from sandweave.sandbox import workspace
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]


@pytest.fixture(scope='module', autouse=True)
def stop_idle_worker():
    yield
    connection = local_connection()
    try:
        connection.call('_shutdown_if_idle')
    finally:
        connection.close()


def test_docker_bulk_download_memory_and_profiling(tmp_path):
    fs = shutil.disk_usage(workspace.local_parent().parent)
    assert fs.free - 12 * 1024**3 >= fs.total * .15
    # The original reproducer: repeated bulk receive through Docker bridge/NAT.
    # TLS verifies the downloads; /dev/null excludes filesystem write growth.
    url = 'https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-9.2.0-linux-x86_64.tar.gz'
    command = ('docker pull python:3.12 && docker run --rm python:3.12 sh -c ' +
               shlex.quote('for i in $(seq 1 12); do curl -fsSL ' + url + ' -o /dev/null || exit; done'))
    with Sandbox(template='docker', profiling=True, cpu=CPU(vcpus=4),
                 memory=Memory('2GiB', '2GiB')) as env, Sandbox(memory='512MiB') as other:
        pid = env.status()['runtime_status']['sentry']['pid']
        env.profile(tmp_path / 'before.pprof')
        process = env.exec(command)
        started = time.monotonic()
        rss, latency = [], []
        try:
            while process.poll() is None:
                fields = dict(line.split(':',1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
                rss.append(int(fields['VmRSS'].split()[0]) * 1024)
                stamp = time.monotonic()
                assert other.run('printf responsive', timeout=10).stdout == 'responsive'
                latency.append(time.monotonic()-stamp)
                fs = shutil.disk_usage(workspace.local_parent().parent)
                assert fs.free - 12 * 1024**3 >= fs.total * .15
                assert time.monotonic()-started < 600
                time.sleep(.5)
            result = process.result()
            assert result.returncode == 0, result.stderr
            asyncio.run(env.profile.aio(tmp_path / 'after.pprof'))
            subprocess.run([sys.executable, '-m', 'sandweave.cli', 'profile', env.id,
                            '--output', str(tmp_path/'cli.pprof')], check=True, capture_output=True)
            for name in ('before','after','cli'):
                assert gzip.decompress((tmp_path/(name+'.pprof')).read_bytes())
            assert max(rss) < 900 * 1024**2
            assert env.run('printf survived', check=True).stdout == 'survived'
            with pytest.raises(UnsupportedFeature, match='profiling=True'):
                other.profile(tmp_path / 'disabled.pprof')
            report = {'downloads': 12, 'seconds': time.monotonic()-started,
                      'max_sentry_rss': max(rss), 'max_unrelated_command_seconds': max(latency)}
            (tmp_path/'network-memory.json').write_text(json.dumps(report))
            print(json.dumps(report))
        finally:
            process.terminate()
