"""Public proxy policies on local pools and two independently running workers."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from sandweave import Cluster, Memory, Network, Pool, ProxyPolicy, Sandbox
from test_proxy_network import proxies, exit_ip, cleanup_worker
from test_weave_live import cluster, wait_for, package_path

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_TEST_PROXIES') or not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'),
    reason='explicit proxy credentials and isolated workers required')]
MEMORY = Memory('256MiB', '256MiB')


@pytest.fixture(scope='module')
def regions(proxies):
    # The supplied test account labels the first two proxies UK and these two US.
    return {'uk': [proxies[i][0] for i in (0, 1)], 'us': [proxies[i][0] for i in (4, 5)]}


@pytest.mark.parametrize('distribution', ['random', 'round_robin', 'same_proxy', 'same_region'])
def test_standalone_region_constraint(regions, proxies, distribution):
    with Sandbox(memory=MEMORY, network=Network(proxy=regions,
                 policy=ProxyPolicy(distribution, region='uk'))) as env:
        observed = exit_ip(env)
        assert observed in {proxies[0][1], proxies[1][1]}
        assert env.info['network']['region'] == 'uk'
        assert env.info['network']['policy']['distribution'] == distribution


def test_region_survives_live_snapshot_and_cache_can_change_region(regions, proxies):
    with Sandbox(memory=MEMORY, network=Network(proxy=regions,
                 policy=ProxyPolicy('same_region', region='uk'))) as env:
        observed = exit_ip(env)
        memory = env.snapshot(state='memory')
        filesystem = env.snapshot(state='filesystem')
    with Sandbox(snapshot=memory) as restored:
        assert exit_ip(restored) == observed
        assert restored.info['network']['region'] == 'uk'
    with Sandbox(cache=filesystem, network=Network(proxy=regions,
                 policy=ProxyPolicy(region='us'))) as changed:
        assert exit_ip(changed) in {proxies[4][1], proxies[5][1]}
    with pytest.raises(ValueError, match='filesystem cache'):
        with Pool(cache=memory):
            pytest.fail('pool must not silently change a live proxy binding')


@pytest.mark.parametrize('template', [False, True])
def test_local_pool_cycles_members_without_counting_builder(regions, proxies, template, tmp_path):
    policy = ProxyPolicy('round_robin', region='uk')
    options = {'network': Network(proxy=regions, policy=policy)}
    if template:
        path = tmp_path / 'proxy.toml'
        path.write_text('extends="coding"\n[resources.network]\n'
                        '[resources.network.proxy]\nuk=' + json.dumps(regions['uk']) + '\n'
                        'us=' + json.dumps(regions['us']) + '\n'
                        '[resources.network.policy]\ndistribution="round_robin"\nregion="uk"\n')
        options = {'template': path}
    with Pool(size=1, memory=MEMORY, **options) as pool:
        actual = []
        for _ in range(3):
            with pool.acquire() as env:
                actual.append(exit_ip(env))
        assert actual == [proxies[0][1], proxies[1][1], proxies[0][1]]


def test_cli_named_pool_applies_distribution_and_region(regions, proxies, tmp_path):
    path = tmp_path / 'proxies.json'
    path.write_text(json.dumps(regions))
    path.chmod(0o600)
    def cli(*args):
        return subprocess.run([sys.executable, '-m', 'sandweave.cli', *args], text=True,
            capture_output=True, check=True, timeout=120,
            env={**os.environ, 'PYTHONPATH': package_path()})
    name = 'proxy-policy-cli'
    try:
        cli('pool', 'create', '--name', name, '--size', '1', '--memory', '256MiB',
            '--runtime-memory', '256MiB', '--proxy-file', str(path),
            '--proxy-policy', 'round_robin', '--proxy-region', 'uk')
        for index in (0, 1):
            result = cli('pool', 'exec', '--no-stdin', name, '--',
                         'curl --fail --silent --max-time 20 https://api.ipify.org')
            assert result.stdout.strip() == proxies[index][1]
    finally:
        cli('pool', 'close', name)


@pytest.mark.parametrize('distribution', ['random', 'round_robin', 'same_proxy', 'same_region'])
def test_cluster_distributes_across_workers(regions, proxies, distribution, cluster):
    policy = ProxyPolicy(distribution, region='uk' if distribution == 'round_robin' else None)
    with Pool(target='weave-live', size=4, warm=4, memory=MEMORY,
              network=Network(proxy=regions, policy=policy)) as pool:
        with ExitStack() as stack:
            envs = [stack.enter_context(pool.acquire()) for _ in range(4)]
            assert len({cluster.connection.call('allocation_get', identity=e.id)['worker'] for e in envs}) == 2
            with ThreadPoolExecutor(4) as executor:
                actual = list(executor.map(exit_ip, envs))
            selected_regions = {env.info['network']['region'] for env in envs}
            assert set(actual) <= {proxies[i][1] for i in (0, 1, 4, 5)}
            if distribution == 'same_proxy':
                assert len(set(actual)) == 1
            elif distribution in ('same_region', 'round_robin'):
                assert len(selected_regions) == 1
                assert sorted(Counter(actual).values()) == [2, 2]


def test_controller_crash_and_pool_reconnect_keep_rotation(regions, proxies, cluster):
    from sandweave.sandbox.ownership import process_alive
    with Pool(target='weave-live', size=1, warm=0, detached=True, memory=MEMORY,
              network=Network(proxy=regions, policy=ProxyPolicy('round_robin', region='uk'))) as pool:
        with pool.acquire() as first:
            assert exit_ip(first) == proxies[0][1]
        wait_for(lambda: pool.info['active'] == 0)
        information = json.loads((Path(cluster.config['directory']) / 'controller.json').read_text())
        command = Path('/proc/' + str(information['pid']) + '/cmdline').read_bytes().split(b'\0')
        assert b'sandweave.weave.server' in command and cluster.config['directory'].encode() in command
        os.kill(information['pid'], signal.SIGKILL)
        wait_for(lambda: process_alive(information['process']) is False)
        replacement = Cluster.start('weave-live', directory=cluster.config['directory'], local_worker=False)
        replacement.close()
        with Pool.connect(pool.id, target='weave-live') as reconnected:
            with reconnected.acquire() as second:
                assert exit_ip(second) == proxies[1][1]
