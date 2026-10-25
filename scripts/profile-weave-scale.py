#!/usr/bin/env python3
"""Time worker admission and the controller's 0.1 s loop with many sandbox records.

Run once per source tree (for example an export of the previous release and the
current checkout). Runtime status queries are stubbed and return immediately,
so admission times are lower bounds: a real status inspects runtime files and
processes for every running sandbox.
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='sandweave source tree (its src directory is imported)')
    parser.add_argument('--historical', type=int, default=17457, help='ended worker records')
    parser.add_argument('--live', type=int, default=500, help='running sandboxes')
    parser.add_argument('--released', type=int, default=3758, help='ended controller allocations')
    parser.add_argument('--repeat', type=int, default=20)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(args.source).resolve() / 'src'))
    from sandweave.sandbox import admission
    from sandweave.sandbox.ownership import process_identity
    from sandweave.sandbox.sandbox import definition
    from sandweave.sandbox.wire import decode, encode
    from sandweave.weave.controller import Controller

    spec = definition()['spec']
    # A describe result is about 6 KB on a desktop worker; pad the copy kept in each allocation.
    info = {'id': None, 'state': 'ready', 'spec': spec, 'runtime_status': {'status': 'running'},
            'timings': {f'phase_{i}': i / 7 for i in range(40)}, 'padding': 'x' * 4000}
    result = {'source': str(Path(args.source).resolve()), 'historical_records': args.historical,
              'live_sandboxes': args.live, 'released_allocations': args.released}
    with tempfile.TemporaryDirectory(prefix='sw-scale-') as temporary:
        root = Path(temporary)
        records = root / 'sandboxes'
        records.mkdir()
        active = set()
        for index in range(args.historical + args.live):
            identity = f'sw-{index:06}'
            state = 'terminated' if index < args.historical else 'ready'
            (records / (identity + '.bin')).write_bytes(encode({'id': identity, 'state': state, 'spec': spec}))
            if state == 'ready':
                active.add(identity)
        worker = SimpleNamespace(records=records, read=lambda i: decode((records / (i + '.bin')).read_bytes()),
                                 path=lambda i: records / (i + '.bin'), active=active, admission_gone=set(),
                                 memory_budget=2**60,
                                 runtime=SimpleNamespace(status=lambda i: {'status': 'running'}))
        durations = []
        for _ in range(args.repeat):
            started = time.perf_counter()
            admission.admit(worker, spec)
            durations.append(time.perf_counter() - started)
        result['admission_seconds'] = statistics.median(durations)

        controller = Controller(root / 'control', connector=lambda *a, **kw: None)
        try:
            owner = 'a' * 32
            controller.owners.register(owner, process_identity())
            now = time.time()
            with controller.state.transaction():
                for index in range(args.released + args.live):
                    identity = f'sw-alloc-{index:06}'
                    ended = index < args.released
                    controller.state.put('allocation', dict(
                        id=identity, state='terminated' if ended else 'ready',
                        desired='terminated' if ended else 'running', released=ended, owner=owner,
                        parent=None, ack=True, deadline=now + 3600, worker='worker-test', generation=1,
                        endpoint={'hostname': 'host', 'port': 1, 'workspace': 'w', 'token': 't'},
                        spec={**spec, 'name': f'machine-{index}'}, request={'spec': spec},
                        info={**info, 'id': identity}))
            controller.next_probe = float('inf')  # Most passes are not probe passes.
            durations = []
            for _ in range(args.repeat):
                started = time.perf_counter()
                controller.tick()
                durations.append(time.perf_counter() - started)
            result['controller_tick_seconds'] = statistics.median(durations)
            # The name check in create() reads every allocation's name.
            durations = []
            for _ in range(args.repeat):
                started = time.perf_counter()
                any(a['spec'].get('name') == 'missing' and not a.get('released')
                    for a in controller.state.list('allocation'))
                durations.append(time.perf_counter() - started)
            result['full_allocation_scan_seconds'] = statistics.median(durations)
            durations = []
            for _ in range(args.repeat):
                started = time.perf_counter()
                any(a['spec.name'] == 'missing'
                    for a in controller.state.list('allocation', released=False, fields=('spec.name',)))
                durations.append(time.perf_counter() - started)
            result['projected_name_scan_seconds'] = statistics.median(durations)
            started = time.perf_counter()
            status = controller.status()
            result['status_seconds'] = time.perf_counter() - started
            result['status_sandboxes'] = len(status['sandboxes'])
            try:
                result['status_bytes'] = len(encode(status))
            except ValueError as error:  # The reply cannot be sent at all.
                result['status_error'] = str(error)
        finally:
            controller.close()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
