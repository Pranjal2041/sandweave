"""Placement planning has no process, transport or database side effects."""
from collections import defaultdict
import time

from ..sandbox.admission import reservation

TERMINAL = frozenset({'terminated', 'stopped', 'failed', 'cancelled', 'succeeded'})


def machine(inventory):
    """One running Linux kernel, independent of worker and PID namespaces.

    If a host hides its boot ID, do not guess identity from a hostname.
    """
    boot = (inventory.get('scope') or {}).get('boot')
    return 'machine-' + boot if boot else None


def charged(record):
    return bool(record.get('worker')) and not record.get('released', False)


def requirements(spec):
    return {'memory': reservation(spec), 'slots': 1,
            'gpu': 1 if spec['resources']['gpu'] else 0}


def plan(requests, workers, allocations, policies, *, now=None):
    """Return (request, worker, GPU UUID) reservations and waiting reasons.

    CPU is shared. A worker's configured slot ceiling bounds concurrency;
    advertised vCPU count is never treated as a dedicated-core reservation.
    """
    now = time.time() if now is None else now
    usage = defaultdict(lambda: {'memory': 0, 'slots': 0, 'gpu': 0})
    shares = defaultdict(lambda: {'memory': 0, 'slots': 0, 'gpu': 0})
    devices = defaultdict(set)
    unknown_devices = set()
    by_worker = {w['id']: w for w in workers}
    localities = defaultdict(dict)
    first_seen = defaultdict(dict)

    def locality(parent, worker_id):
        if policies.get(parent, {}).get('affinity') == 'machine':
            worker = by_worker.get(worker_id, {})
            return worker.get('machine') or machine(worker.get('inventory', {})) or worker_id
        return worker_id

    def remember(parent, worker_id):
        if not policies.get(parent, {}).get('affinity'):
            return
        place = locality(parent, worker_id)
        localities[parent].setdefault(place, len(localities[parent]))

    # Keep the initial builder's locality after it stops. Idle pools retain
    # their preference, and a restart reconstructs it from durable records.
    for record in allocations:
        parent = record.get('parent')
        if (policies.get(parent, {}).get('affinity') and record.get('worker') and
                (charged(record) or record.get('prepared'))):
            place = locality(parent, record['worker'])
            age = (record.get('created', 0), record['id'])
            first_seen[parent][place] = min(first_seen[parent].get(place, age), age)
        if charged(record):
            need = requirements(record['spec'])
            if need['gpu']:
                if record['spec'].get('_gpu_uuid'):
                    devices[record['worker']].add(record['spec']['_gpu_uuid'])
                else:
                    unknown_devices.add(record['worker'])
            for resource in need:
                usage[record['worker']][resource] += need[resource]
                shares[record.get('parent') or 'default'][resource] += need[resource]
    for parent, places in first_seen.items():
        localities[parent] = {place: i for i, place in enumerate(sorted(places, key=places.get))}
    available = [w for w in workers if w['state'] == 'ready' and not w.get('draining')]
    totals = {r: max(1, sum(w['capacity'][r] for w in available)) for r in ('memory', 'slots', 'gpu')}
    pending, assignments, waiting = list(requests), [], {}

    def rank(request):
        policy = policies.get(request.get('parent'), {})
        share = shares[request.get('parent') or 'default']
        dominant = max(share[r] / totals[r] for r in totals) / policy.get('weight', 1)
        # Priority governs urgency. Within a priority, weighted shares govern
        # admission and age breaks ties, including large pending requests.
        return (-policy.get('priority', 0), dominant, request['created'], request['id'])

    while pending:
        pending.sort(key=rank)
        request = pending.pop(0)
        need = requirements(request['spec'])
        candidates, reasons = [], set()
        policy = policies.get(request.get('parent'), {})
        limit = policy.get('quota')
        if limit is not None and shares[request.get('parent') or 'default']['slots'] >= limit:
            waiting[request['id']] = 'pool quota is fully reserved'
            continue
        for worker in available:
            identity, capacity = worker['id'], worker['capacity']
            selected = None
            if request.get('only_worker') and identity != request['only_worker']:
                continue
            labels = policy.get('labels', {})
            if any(worker.get('labels', {}).get(k) != v for k, v in labels.items()):
                reasons.add('worker labels do not match')
                continue
            if request['spec']['runtime'] not in worker.get('runtimes', ('gvisor', 'apptainer')):
                reasons.add('runtime is unavailable')
                continue
            gpu = request['spec']['resources']['gpu']
            if gpu:
                external = worker.get('external', {})
                occupied = devices[identity] | set(external.get('gpu_uuids', []))
                matching = [g['uuid'] for g in worker.get('gpus', []) if g['uuid'] not in occupied and
                            (not gpu.get('model') or gpu['model'].lower() in g['model'].lower())]
                if (not matching or identity in unknown_devices or
                        external.get('gpu', 0) > len(external.get('gpu_uuids', []))):
                    reasons.add('no unreserved GPU matches the request')
                    continue
                selected = matching[0]
            if any(need[r] + usage[identity][r] + worker.get('external', {}).get(r, 0) > capacity[r] for r in need):
                reasons.add('worker resources are reserved')
                continue
            load = max(usage[identity][r] / max(1, capacity[r]) for r in need)
            preference = 0
            if policy.get('affinity'):
                places = localities[request.get('parent')]
                place = locality(request.get('parent'), identity)
                preference = places.get(place, len(places))
            candidates.append((preference, load if policy.get('placement', 'spread') == 'spread' else -load,
                               identity, selected))
        if not candidates:
            waiting[request['id']] = '; '.join(sorted(reasons)) or 'no ready worker matches this request'
            continue
        _, _, worker_id, selected = min(candidates)
        assignments.append((request['id'], worker_id, selected))
        remember(request.get('parent'), worker_id)
        if selected:
            devices[worker_id].add(selected)
        for r in need:
            usage[worker_id][r] += need[r]
            shares[request.get('parent') or 'default'][r] += need[r]
    return assignments, waiting
