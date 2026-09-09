"""Small public summaries of a worker's sandbox records."""
import copy
import shlex


def summarize(record, *, ssh_host=None):
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
            host = ssh_host or hostname
            vnc = {'url': f'vnc://127.0.0.1:{port}', 'port': port,
                   'worker_host': hostname,
                   'ssh_command': shlex.join(['ssh', '-N', '-o', 'ExitOnForwardFailure=yes',
                       '-L', f'127.0.0.1:{port}:127.0.0.1:{port}', host]) if host else None}
    return copy.deepcopy({
        'id': record['id'], 'name': record.get('name'), 'state': record['state'],
        'template': spec['template']['name'], 'runtime': spec['runtime'],
        'worker': {'hostname': hostname, 'job_id': worker.get('job_id')},
        'cpu': resources['cpu'], 'memory': resources['memory'],
        'gpus': gpus,
        'vnc': vnc,
    })
