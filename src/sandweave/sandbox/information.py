"""Small public summaries of a worker's sandbox records."""
import copy


def summarize(record):
    spec = record['spec']
    runtime = record.get('runtime_status', {})
    worker = record.get('worker', {})
    hostname = worker.get('hostname') or runtime.get('launcher', {}).get('hostname')
    resources = spec['resources']
    desktop = spec['template'].get('capabilities', {}).get('desktop')
    gpus = (runtime.get('gpus', None if resources.get('gpu') else [])
            if runtime.get('status') in ('running', 'paused') else [])
    vnc = None
    # The engine also reserves port 5901 for coding guests. A forwarded port
    # alone does not mean that this template runs a VNC server.
    if (desktop is not None and desktop.get('provider', 'desktop') == 'desktop'
            and desktop.get('backend', 'xvnc') == 'xvnc'
            and record['state'] == 'ready' and runtime.get('status') == 'running'):
        port = runtime.get('ports', {}).get('5901')
        if port is not None:
            port = int(port)
            vnc = {'url': f'vnc://127.0.0.1:{port}', 'port': port,
                   'worker_host': hostname, 'password': None}
    return copy.deepcopy({
        'id': record['id'], 'name': record.get('name'), 'state': record['state'],
        'template': spec['template']['name'], 'runtime': spec['runtime'],
        'worker': {'hostname': hostname},
        'cpu': resources['cpu'], 'memory': resources['memory'],
        **({'disk_memory': {key: runtime['disk_memory'].get(key)
                           for key in ('directory', 'host_limit_bytes')}} if runtime.get('disk_memory') else {}),
        'gpus': gpus,
        'vnc': vnc,
        **({'image': {key: spec['image'][key] for key in ('reference', 'digest', 'platform')}}
           if spec.get('image') else {}),
    })
