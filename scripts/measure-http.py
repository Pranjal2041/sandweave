#!/usr/bin/env python3
"""Host wall-clock timing of a forwarded Moodle page, including the SSH tunnel."""
import argparse
import http.client
import json
import statistics
import time
p = argparse.ArgumentParser()
p.add_argument('--port', type=int, default=28082)
p.add_argument('--trials', type=int, default=10)
p.add_argument('--path', default='/login/index.php')
p.add_argument('--label', required=True)
a = p.parse_args()
measurements = []
for trial in range(a.trials):
    conn = http.client.HTTPConnection('127.0.0.1', a.port, timeout=60)
    start = time.perf_counter()
    conn.request('GET', a.path, headers={'Host': 'localhost'})
    response = conn.getresponse()
    body = response.read()
    elapsed = time.perf_counter() - start
    assert response.status == 200, (response.status, body[:300])
    assert b'logintoken' in body and b'Moodle' in body, body[:300]
    row = {'trial': trial, 'seconds': elapsed, 'bytes': len(body)}
    print(json.dumps(row), flush=True)
    measurements.append(elapsed)
    conn.close()
print(json.dumps({'label': a.label, 'trials': a.trials, 'first_seconds': measurements[0],
                  'warm_median_seconds': statistics.median(measurements[1:]),
                  'warm_max_seconds': max(measurements[1:]),
                  'measurement': 'host wall clock; PHP-rendered login page; includes SSH tunnel'}))
