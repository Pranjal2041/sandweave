#!/usr/bin/env python3
"""Measure a full desktop round trip while post-save verification runs separately."""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

lab = Path(__file__).resolve().parent.parent
local = Path((lab / 'runs/local-path.txt').read_text().strip())
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('name')
p.add_argument('--keep', action='store_true', help='leave test clones running for VNC inspection')
a = p.parse_args()
names = [a.name + '-src', a.name + '-dst']
snapshot = lab / 'snapshots' / a.name
result = {'source': names[0], 'restored': names[1], 'snapshot': str(snapshot)}


def ports(name):
    return json.loads((lab / 'runs/gvisor' / name / 'ports.json').read_text())


def probe(name):
    with urllib.request.urlopen('http://127.0.0.1:' + str(ports(name)['8000']), timeout=1) as response:
        return json.load(response)


def launch(name, image):
    start = time.perf_counter()
    subprocess.run([sys.executable, 'scripts/run-gvisor.py', '--detach', '--cpus', '4-7',
                    '--restore', str(image), name], cwd=lab, check=True)
    logs = lab / 'runs/gvisor' / name
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if (logs / 'exit-code.txt').exists():
            raise RuntimeError((logs / 'launcher.out').read_text()[-4000:])
        try:
            state = probe(name)
            elapsed = time.perf_counter() - start
            print(name, 'state probe ready in', elapsed, 'seconds', flush=True)
            return {'service_ready_seconds': elapsed,
                    'launcher': json.loads((logs / 'restore-timings.json').read_text())}, state
        except (OSError, ValueError):
            time.sleep(.05)
    raise TimeoutError('restore readiness: ' + name)


def guest(name, command):
    settings = json.loads((local / 'gvisor/bundles' / name / 'launch-settings.json').read_text())
    return subprocess.check_output(['scripts/gvisor-host.sh', '/lab/' + settings['runtime']['path'] + '/runsc',
                                    '--root=/local/gvisor/state', 'exec', name, *command], cwd=lab, timeout=30)


try:
    result['legacy_restore'], before = launch(names[0], lab / 'snapshots/full-desktop-ready')
    start = time.perf_counter()
    saved = subprocess.check_output([sys.executable, 'scripts/checkpoint-gvisor.py', names[0], a.name],
                                    cwd=lab, timeout=180)
    result['save_command_seconds'] = time.perf_counter() - start
    result['save'] = json.loads(saved)
    result['verification_at_save_return'] = json.loads((snapshot / 'verification.json').read_text())
    print('Save returned:', result['save_command_seconds'], 'verification:', result['verification_at_save_return']['status'], flush=True)
    request = urllib.request.Request('http://127.0.0.1:' + str(ports(names[0])['8000']), data=b'', method='POST')
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 204
    result['new_restore'], after = launch(names[1], snapshot)
    result['verification_at_restore_ready'] = json.loads((snapshot / 'verification.json').read_text())
    for key in ('nonce', 'pid', 'unlinked_file', 'file_offset', 'marker', 'uid', 'gid', 'mode', 'cross_netns_tcp', 'abstract_queued'):
        assert after[key] == before[key], (key, before[key], after[key])
    assert after['counter'] < 1000000 and probe(names[0])['counter'] >= 1000000
    result['probe'] = after
    assert guest(names[1], ['curl', '--max-time', '10', '-s', '-o', '/dev/null', '-w', '%{http_code}', 'http://localhost/login/index.php']) == b'200'
    nested = guest(names[1], ['docker', 'exec', 'general-vm-moodle', 'docker', 'ps', '--format', '{{.Names}} {{.Status}}']).decode()
    assert 'moodle-mariadb Up' in nested, nested
    assert b'firefox-bin -contentproc' in guest(names[1], ['pgrep', '-af', 'firefox'])
    with socket.create_connection(('127.0.0.1', ports(names[1])['5901']), timeout=5) as connection:
        assert connection.recv(12).startswith(b'RFB ')
    result.update(moodle_http=200, nested_docker=nested, vnc_port=ports(names[1])['5901'])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        verification = json.loads((snapshot / 'verification.json').read_text())
        if verification['status'] in ('passed', 'failed'):
            break
        time.sleep(.2)
    assert verification['status'] == 'passed', verification
    result.update(passed=True, verification=verification)
    (lab / 'runs/gvisor' / names[1] / 'snapshot-performance.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2), flush=True)
finally:
    if not a.keep:
        for name in names:
            control = local / 'gvisor/state' / ('runsc-' + name + '.sock')
            if control.exists():
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(5)
                    connection.connect(str(control))
                    connection.sendall(json.dumps({'method': 'containerManager.Signal', 'arg': {'CID': name, 'Signo': 9, 'PID': 0, 'Mode': 1}}).encode())
                    connection.recv(8192)
