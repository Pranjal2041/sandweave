"""Live proxy egress acceptance, using an explicit private credential file."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest

from sandweave import Network, Sandbox

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_TEST_PROXIES'), reason='explicit proxy credentials required')]


@pytest.fixture(scope='module')
def proxies():
    records = json.loads(Path(os.environ['SANDWEAVE_TEST_PROXIES']).read_text())
    return [(f"http://{quote(p['username'],safe='')}:{quote(p['password'],safe='')}@{p['host']}:{p['port']}",
             p['host']) for p in records]


@pytest.fixture(scope='module', autouse=True)
def cleanup_worker():
    yield
    from sandweave.sandbox.targets import local_connection
    connection = local_connection()
    try:
        connection.call('_shutdown_if_idle')
    finally:
        connection.close()


def exit_ip(env):
    result = env.run('curl --fail --silent --show-error --max-time 20 https://api.ipify.org',timeout=25)
    assert result.returncode == 0, 'proxy request failed'
    return result.stdout.strip()


def test_separate_sandboxes_use_separate_exits_and_cannot_bypass(proxies):
    def check(proxy):
        url, expected = proxy
        with Sandbox(memory='256MiB', network=Network(proxy=url)) as env:
            assert exit_ip(env) == expected
            assert expected in env.info['network']['proxy']
            assert '@' not in env.info['network']['proxy']
            for target in ['https://1.1.1.1/cdn-cgi/trace','https://example.com']:
                result = env.run("curl --noproxy '*' --max-time 3 --silent " + target,timeout=5)
                assert result.returncode != 0, 'proxy mode allowed direct egress'
            assert env.run('echo control-still-works').stdout == 'control-still-works\n'
            return expected
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert len(set(executor.map(check,proxies[:2]))) == 2


def test_proxy_setup_cache_live_restore_and_offline_override(proxies, tmp_path):
    url, expected = proxies[0]
    script = tmp_path / 'setup.sh'
    script.write_text('set -eu\ncurl --fail --silent --max-time 20 https://api.ipify.org > /workspace/setup-ip\n')
    with Sandbox(memory='256MiB', network=Network(proxy=[url]), setup=str(script)) as original:
        assert original.files.read_text('/workspace/setup-ip').strip() == expected
        saved = original.snapshot(state='memory')
        assert saved.verify()['status'] == 'passed'
        cache = original.snapshot(state='filesystem')
    with Sandbox(snapshot=saved) as restored:
        assert exit_ip(restored) == expected
    with Sandbox(cache=cache, network=Network(proxy=proxies[1][0])) as clone:
        assert exit_ip(clone) == proxies[1][1]
        assert clone.files.read_text('/workspace/setup-ip').strip() == expected
    with Sandbox(cache=cache, network='offline') as offline:
        assert 'network' not in offline.info
        assert offline.run("test -z \"${http_proxy:-}\"").returncode == 0
        assert offline.run('curl --silent --max-time 3 https://api.ipify.org',timeout=5).returncode != 0
