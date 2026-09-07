#!/usr/bin/env python3
"""Verify host isolation and public connectivity on an already-running lab guest."""
import argparse
import json
from pathlib import Path
import socket
import subprocess

lab = Path(__file__).resolve().parent.parent
local = Path((lab / 'runs/local-path.txt').read_text().strip())
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('name')
p.add_argument('--docker', action='store_true')
a = p.parse_args()
settings = json.loads((local / 'gvisor/bundles' / a.name / 'launch-settings.json').read_text())
runtime = '/lab/' + settings['runtime']['path'] + '/runsc'
prefix = ['scripts/gvisor-host.sh', runtime, '--root=/local/gvisor/state', 'exec', a.name]
policy = json.loads((local / 'gvisor/bundles' / a.name / 'network-policy.json').read_text())
results = {}
with socket.socket() as canary:
    canary.bind(('0.0.0.0', 0))
    canary.listen(64)
    port = canary.getsockname()[1]
    with socket.create_connection(('127.0.0.1', port), timeout=2):
        c, _ = canary.accept()
        c.close()
    addresses = sorted(set(policy['host_addresses']) - {'127.0.0.1'})
    targets = [(addr, port) for addr in ['10.0.2.2', *addresses]]
    targets += [('10.0.2.2', 40377), ('10.0.2.2', 40047)]
    code = '''import socket,json
results={}
for address,port in TARGETS:
 try:
  s=socket.create_connection((address,port),timeout=.7);s.close();results[address+':'+str(port)]='CONNECTED'
 except OSError as e:results[address+':'+str(port)]=type(e).__name__
print(json.dumps(results))
'''.replace('TARGETS', repr(targets))
    result = subprocess.run(prefix + ['python3', '-c', code], cwd=lab, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
    result.check_returncode()
    results['guest_host_denials'] = json.loads(result.stdout)
    assert 'CONNECTED' not in results['guest_host_denials'].values(), results
    if a.docker:
        for label, inner in [('docker', ['docker', 'exec', 'general-vm-moodle']),
                             ('nested_docker', ['docker', 'exec', 'general-vm-moodle', 'docker', 'exec', 'moodle-mariadb'])]:
            command = inner + ['bash', '-c', f'timeout 1 bash -c "exec 9<>/dev/tcp/10.0.2.2/{port}"']
            r = subprocess.run(prefix + command, cwd=lab, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=8)
            results[label + '_host_canary'] = {'exit': r.returncode, 'output': r.stdout.decode()}
            assert r.returncode == 124, results
    canary.settimeout(.1)
    try:
        connection, address = canary.accept()
        connection.close()
        raise AssertionError('guest reached host canary: ' + str(address))
    except TimeoutError:
        pass
if policy['mode'] == 'internet':
    for label, inner in [('guest', []), *([('docker', ['docker', 'exec', 'general-vm-moodle'])] if a.docker else [])]:
        r = subprocess.run(prefix + inner + ['curl', '--max-time', '15', '-s', '-o', '/dev/null', '-w', '%{http_code}', 'https://example.com'], cwd=lab, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
        results[label + '_public_https'] = r.stdout.decode()
        assert r.returncode == 0 and r.stdout == b'200', results
else:
    code = '''import socket,json
results={}
try:
 s=socket.create_connection(('1.1.1.1',443),timeout=1);s.close();results['public_tcp']='CONNECTED'
except OSError as e:results['public_tcp']=type(e).__name__
with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as s:
 s.settimeout(1)
 s.sendto(bytes.fromhex('123401000001000000000000076578616d706c6503636f6d0000010001'),('10.0.2.3',53))
 try:s.recv(1024);results['dns']='RESPONSE'
 except OSError as e:results['dns']=type(e).__name__
print(json.dumps(results))
'''
    r = subprocess.run(prefix + ['python3', '-c', code], cwd=lab, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=10)
    r.check_returncode()
    results['offline_egress'] = json.loads(r.stdout)
    assert results['offline_egress'] == {'public_tcp': 'TimeoutError', 'dns': 'TimeoutError'}, results
(lab / 'runs/gvisor' / a.name / 'network-acceptance.json').write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps(results, indent=2))
