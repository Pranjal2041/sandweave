#!/usr/bin/env python3
"""Disposable GPU pause/resume, filesystem stop, and experimental CUDA restore."""
import argparse
import base64
import json
import socket
import time
import urllib.request
import uuid

from environment import EnvironmentManager
import snapshot_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gpu', type=int, required=True)
    parser.add_argument('--cpus', default='4-7')
    args = parser.parse_args()
    manager = EnvironmentManager()
    prefix = 'lifecycle-gpu-' + uuid.uuid4().hex[:8]
    names = [prefix + '-' + suffix for suffix in ('desktop', 'cold', 'cuda', 'live')]
    desktop, cold, cuda, live = names
    options = ['--gpu', str(args.gpu), '--cpus', args.cpus, '--guest-gs',
               '--memory-mib', '4096', '--runtime-memory-mib', '512', '--no-runtime-debug']
    report = {'prefix': prefix, 'hostname': socket.gethostname(), 'cycles': []}

    def guest(name, *command):
        return manager._run([*manager._command(name), 'exec', name, *command])

    def probe(name):
        port = manager.status(name)['ports']['8000']
        with urllib.request.urlopen(f'http://127.0.0.1:{port}', timeout=3) as response:
            return json.load(response)

    def wait(operation, timeout=60):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                value = operation()
                if value:
                    return value
            except (OSError, RuntimeError, StopIteration) as error:
                last = error
            time.sleep(.2)
        raise TimeoutError(str(last))

    def interop():
        output = guest(desktop, 'journalctl', '-u', 'lifecycle-interop', '--no-pager', '-n', '5', '-o', 'cat')
        rows = [json.loads(line) for line in output.splitlines() if line.startswith('{')]
        return next(row for row in reversed(rows) if row['phase'] == 'interop_alive')

    try:
        manager.start(desktop, options=options)
        wait(lambda: guest(desktop, 'systemctl', 'is-active', 'tigervncserver@:1.service').strip() == 'active')
        for source, target in [('gpu-checkpoint-cuda-probe.py', '/opt/lifecycle-cuda.py'),
                               ('gpu-glx-interop-probe.py', '/opt/lifecycle-interop.py')]:
            data = base64.b64encode((manager.lab / 'scripts' / source).read_bytes()).decode()
            guest(desktop, 'python3', '-c',
                  f'import base64,pathlib;pathlib.Path({target!r}).write_bytes(base64.b64decode({data!r}))')
        guest(desktop, 'systemd-run', '--unit=lifecycle-cuda', 'python3', '/opt/lifecycle-cuda.py')
        guest(desktop, 'systemd-run', '--unit=lifecycle-interop', '--uid=ga', '--setenv=DISPLAY=:1',
              '--setenv=XAUTHORITY=/home/ga/.Xauthority', '/usr/local/bin/engine-gpu-gl', '-nodl',
              'python3', '/opt/lifecycle-interop.py', '--hold-interop')
        before, gl_before = wait(lambda: probe(desktop)), wait(interop)
        for _ in range(3):
            tick = time.monotonic()
            assert manager.pause(desktop)['status'] == 'paused'
            pause = time.monotonic() - tick
            time.sleep(2)
            tick = time.monotonic()
            assert manager.resume(desktop)['status'] == 'running'
            resume = time.monotonic() - tick
            after = probe(desktop)
            for key in ('nonce', 'pid', 'address', 'value'):
                assert after[key] == before[key]
            time.sleep(2.1)
            gl_after = interop()
            assert gl_after['pid'] == gl_before['pid'] and gl_after['value'] == 42
            assert gl_after['time'] > gl_before['time']
            report['cycles'].append({'pause_seconds': pause, 'resume_seconds': resume,
                                      'cuda': after, 'interop': gl_after})
            gl_before = gl_after
        with socket.create_connection(('127.0.0.1', manager.status(desktop)['ports']['5901']), timeout=3) as conn:
            report['vnc_banner'] = conn.recv(12).decode()
        assert report['vnc_banner'].startswith('RFB')
        guest(desktop, 'sh', '-c', 'printf persistent-lifecycle-gpu > /opt/lifecycle-marker')
        stopped = manager.stop(desktop, label=prefix + '-fs')
        assert stopped['last_stop']['saved']['kind'] == 'filesystem'
        manager.load(stopped['last_stop']['saved']['snapshot'], cold, options=['--cpus', args.cpus])
        assert guest(cold, 'cat', '/opt/lifecycle-marker') == 'persistent-lifecycle-gpu'
        guest(cold, 'sh', '-c', 'mkdir -p /mnt/lifecycle-unknown; mount -t tmpfs tmpfs /mnt/lifecycle-unknown')
        try:
            manager.stop(cold, label=prefix + '-rejected')
        except RuntimeError as error:
            assert 'unsupported writable mount' in str(error)
        else:
            raise AssertionError('unknown writable mount was silently omitted')
        assert manager.status(cold)['status'] == 'running'
        assert guest(cold, 'cat', '/opt/lifecycle-marker') == 'persistent-lifecycle-gpu'
        assert not (manager.lab / 'snapshots' / (prefix + '-rejected')).exists()
        report.update(filesystem_stop=stopped, failed_save_preserved_source=True)
        manager.stop(cold, discard=True)
        code = (manager.lab / 'scripts/gpu-checkpoint-cuda-probe.py').read_text()
        manager.start(cuda, command=['python3', '-u', '-c', code], options=options)
        cuda_before = wait(lambda: probe(cuda))
        manager.pause(cuda)
        saved = manager.save(cuda, prefix + '-cuda-paused', mode='experimental-gpu-live')
        assert manager.status(cuda)['status'] == 'paused'
        try:
            manager.load(saved['snapshot'], live)
        except ValueError as error:
            assert 'experimental_gpu_live' in str(error)
        else:
            raise AssertionError('GPU live restore allowed without opt-in')
        assert not manager._bundle(live).exists()
        manager.resume(cuda)
        assert probe(cuda)['nonce'] == cuda_before['nonce']
        cuda_stop = manager.stop(cuda, label=prefix + '-cuda-stop', mode='experimental-gpu-live')
        manager.load(cuda_stop['last_stop']['saved']['snapshot'], live,
                     experimental_gpu_live=True, options=['--cpus', args.cpus])
        cuda_after = wait(lambda: probe(live))
        for key in ('nonce', 'pid', 'address', 'value'):
            assert cuda_after[key] == cuda_before[key]
        report.update(passed=True, experimental_cuda={'before': cuda_before, 'restored': cuda_after,
                      'saved_while_paused': saved, 'stopped': cuda_stop, 'missing_opt_in_refused': True})
    finally:
        for name in reversed(names):
            if manager.status(name)['status'] in ('running', 'paused', 'starting'):
                manager.stop(name, discard=True)
    snapshot_store.write_json(manager.lab / 'runs/lifecycle-gpu-suite.json', report)
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
