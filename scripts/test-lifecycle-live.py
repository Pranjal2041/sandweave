#!/usr/bin/env python3
"""Disposable live lifecycle acceptance, including a shared-controller peer."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
import uuid

from environment import EnvironmentManager
import environment_control as control
import snapshot_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cpus', default='4-7')
    args = parser.parse_args()
    manager = EnvironmentManager()
    prefix = 'lifecycle-' + uuid.uuid4().hex[:8]
    source, peer, clone = [prefix + '-' + suffix for suffix in ('source', 'peer', 'clone')]
    options = ['--cpus', args.cpus, '--guest-cpus', '2', '--memory-mib', '512',
               '--runtime-memory-mib', '256', '--cpu-policy', 'quota', '--cpu-quota', '0.5',
               '--no-runtime-debug', '--guest-gs']
    code = (manager.lab / 'scripts/snapshot-probe.py').read_text()
    started = []
    report = {'prefix': prefix, 'cpus': args.cpus, 'hostname': socket.gethostname()}

    def probe(name):
        port = manager.status(name)['ports']['8000']
        with urllib.request.urlopen(f'http://127.0.0.1:{port}', timeout=3) as response:
            return json.load(response)

    def ready(name):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                return probe(name)
            except OSError:
                time.sleep(.1)
        raise TimeoutError('probe did not become ready: ' + name)

    def timed(key, call):
        tick = time.monotonic()
        value = call()
        report[key] = time.monotonic() - tick
        return value

    try:
        for name in (source, peer):
            started.append(name)
            manager.start(name, command=['python3', '-u', '-c', code], options=options)
            ready(name)
        manager._run([*manager._command(source), 'exec', source, 'sh', '-c', 'test ! -e /dev/kvm'])
        before, peer_before = probe(source), probe(peer)
        assert timed('pause_seconds', lambda: manager.pause(source))['status'] == 'paused'
        assert manager.pause(source)['status'] == 'paused'
        with control.acquire_lock(manager.local, source):
            collision = subprocess.run(['python', str(manager.lab / 'scripts/checkpoint-gvisor.py'),
                                        source, prefix + '-collision'], capture_output=True, text=True)
            assert collision.returncode and 'in progress' in collision.stderr
        time.sleep(3)
        try:
            probe(source)
        except OSError:
            pass
        else:
            raise AssertionError('paused guest answered an HTTP request')
        assert probe(peer)['counter'] - peer_before['counter'] >= 20
        saved = timed('save_paused_seconds', lambda: manager.save(source, prefix + '-saved'))
        assert manager.status(source)['status'] == 'paused'
        assert list((manager.local / 'gvisor/cpu-brokers').glob('*/job-' + source + '.json.suspend'))
        assert timed('resume_seconds', lambda: manager.resume(source))['status'] == 'running'
        assert manager.resume(source)['status'] == 'running'
        after = probe(source)
        assert after['counter'] - before['counter'] < 12, (before, after)
        assert after['nonce'] == before['nonce']
        url = f"http://127.0.0.1:{manager.status(source)['ports']['8000']}"
        with urllib.request.urlopen(urllib.request.Request(url, data=b'', method='POST'), timeout=3) as response:
            assert response.status == 204
        assert probe(source)['marker'] == 'changed-after-checkpoint'
        broker_file = next((manager.local / 'gvisor/cpu-brokers').glob('*/job-' + source + '.json')).parent / 'status.json'
        broker_pid = json.loads(broker_file.read_text())['pid']
        stopped = timed('stop_with_save_seconds', lambda: manager.stop(source, label=prefix + '-stop'))
        assert stopped['status'] == 'stopped' and stopped['last_stop']['complete']
        assert manager.stop(source)['status'] == 'stopped'
        assert not list((manager.local / 'gvisor/network' / source).glob('*.sock'))
        assert not list((manager.local / 'gvisor/cpu-brokers').glob('*/job-' + source + '.json'))
        assert json.loads(broker_file.read_text())['pid'] == broker_pid
        assert probe(peer)['nonce'] == peer_before['nonce']
        started.append(clone)
        timed('load_after_source_stop_seconds', lambda: manager.load(saved['snapshot'], clone, options=options))
        restored = ready(clone)
        for key in ('nonce', 'pid', 'unlinked_file', 'file_offset', 'marker', 'uid', 'gid', 'mode', 'cross_netns_tcp', 'abstract_queued'):
            assert restored[key] == before[key], (key, before[key], restored[key])
        manager.pause(clone)
        assert timed('discard_paused_seconds', lambda: manager.stop(clone, discard=True))['status'] == 'stopped'
        assert probe(peer)['nonce'] == peer_before['nonce']
        report.update(passed=True, before=before, after_resume=after, restored=restored,
                      saved=saved, stopped=stopped, shared_broker_survived=broker_pid,
                      kvm_absent=True, peer_survived=True)
        snapshot_store.write_json(manager.lab / 'runs/lifecycle-acceptance.json', report)
        print(json.dumps(report, indent=2), flush=True)
    finally:
        for name in reversed(started):
            if manager.status(name)['status'] in ('running', 'paused', 'starting'):
                manager.stop(name, discard=True)


if __name__ == '__main__':
    main()
