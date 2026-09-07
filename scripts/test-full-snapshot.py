#!/usr/bin/env python3
"""Live lab acceptance: restore an unchanged Linux desktop and nested Docker."""
import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

lab = Path(__file__).resolve().parent.parent
local = Path((lab / 'runs/local-path.txt').read_text().strip())
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('name')
p.add_argument('--durable', action='store_true')
a = p.parse_args()
original, restored = a.name, 'restored-' + a.name
label = a.name + '-whole'
logs = lab / 'runs/gvisor' / original

def call(command, timeout=30):
    result = subprocess.run(command, cwd=lab, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{command[:6]}: {result.stdout.decode(errors="replace")[-4000:]}')
    return result.stdout

def guest(name, command, timeout=30):
    settings = json.loads((local / 'gvisor/bundles' / name / 'launch-settings.json').read_text())
    runtime = '/lab/' + settings['runtime']['path'] + '/runsc'
    return call(['scripts/gvisor-host.sh', runtime, '--root=/local/gvisor/state', 'exec', name, *command], timeout)

def wait_for(description, operation, timeout=90):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = operation()
            if result:
                return result
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            last = error
        time.sleep(.5)
    raise TimeoutError(f'{description}: {last}')

def port(name, number):
    return json.loads((lab / 'runs/gvisor' / name / 'ports.json').read_text())[str(number)]

def probe(name):
    with urllib.request.urlopen(f'http://127.0.0.1:{port(name, 8000)}', timeout=5) as response:
        return json.load(response)

print(call(['python', 'scripts/run-gvisor.py', '--detach', '--cpus', '4-7', '--nftables', '--guest-gs',
            '--cgroup', 'v1', '--docker-data', '--docker-archive', 'images/gvisor-moodle-persisted-docker.tar',
            original, '--', '/usr/local/bin/engine-docker', 'init']).decode(), flush=True)
wait_for('Docker boot', lambda: b'ServerVersion' in guest(original, ['docker', 'info', '--format', '{{json .}}'], timeout=5))
guest(original, ['docker', 'start', 'general-vm-moodle'])
wait_for('Moodle boot', lambda: guest(original, ['curl', '--max-time', '3', '-s', '-o', '/dev/null', '-w', '%{http_code}', 'http://localhost/login/index.php'], timeout=5) == b'200')
source = base64.b64encode((lab / 'scripts/snapshot-probe.py').read_bytes()).decode()
guest(original, ['python3', '-c', "import pathlib,base64;pathlib.Path('/opt/snapshot-probe.py').write_bytes(base64.b64decode(" + repr(source) + "))"])
guest(original, ['systemd-run', '--unit=snapshot-probe', 'python3', '/opt/snapshot-probe.py'])
guest(original, ['sh', '-c', 'install -d -m700 -oga -gga /home/ga/engine-firefox; systemd-run --unit=checkpoint-firefox --uid=ga --setenv=DISPLAY=:1 --setenv=XAUTHORITY=/home/ga/.Xauthority /opt/firefox/firefox --no-remote --profile /home/ga/engine-firefox http://localhost/login/index.php'])
before = wait_for('state probe', lambda: probe(original))
wait_for('Firefox', lambda: b'firefox-bin -contentproc' in guest(original, ['pgrep', '-af', 'firefox'], timeout=5))
(logs / 'probe-before.json').write_text(json.dumps(before, indent=2) + '\n')
print('Original Moodle, nested Docker, Firefox and state probe ready', flush=True)
print(call(['python', 'scripts/checkpoint-gvisor.py', *([] if a.durable else ['--local-only']), original, label], timeout=240).decode(), flush=True)
checkpoint = lab / 'snapshots' / label if a.durable else local / 'gvisor/checkpoints' / label
req = urllib.request.Request(f'http://127.0.0.1:{port(original, 8000)}', data=b'', method='POST')
with urllib.request.urlopen(req, timeout=5) as response:
    assert response.status == 204
print(call(['python', 'scripts/run-gvisor.py', '--detach', '--cpus', '4-7', '--restore', str(checkpoint), restored], timeout=90).decode(), flush=True)
after = wait_for('restored state probe', lambda: probe(restored), timeout=90)
(logs / 'probe-restored.json').write_text(json.dumps(after, indent=2) + '\n')
for key in ('nonce', 'pid', 'unlinked_file', 'file_offset', 'marker', 'uid', 'gid', 'mode', 'cross_netns_tcp', 'abstract_queued'):
    assert after[key] == before[key], (key, before[key], after[key])
assert after['counter'] < 1000000, after
assert probe(original)['counter'] >= 1000000 and probe(original)['marker'] == 'changed-after-checkpoint'
web = guest(restored, ['curl', '--max-time', '10', '-s', '-o', '/dev/null', '-w', '%{http_code}', 'http://localhost/login/index.php'])
assert web == b'200', web
nested = guest(restored, ['docker', 'exec', 'general-vm-moodle', 'docker', 'ps', '--format', '{{.Names}} {{.Status}}'])
assert b'moodle-mariadb Up' in nested, nested
internet = guest(restored, ['curl', '--max-time', '15', '-s', '-o', '/dev/null', '-w', '%{http_code}', 'https://example.com'])
assert internet == b'200', internet
assert b'firefox-bin -contentproc' in guest(restored, ['pgrep', '-af', 'firefox'])
result = {'original': original, 'restored': restored, 'checkpoint': str(checkpoint), 'probe': after,
          'moodle_http': web.decode(), 'internet_http': internet.decode(), 'nested_docker': nested.decode(),
          'vnc_port': port(restored, 5901), 'visual_verification': 'pending actual screenshot/navigation review'}
(logs / 'snapshot-acceptance.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2), flush=True)
