"""Build an explicit, version-specific SecCheck coverage manifest at launch."""
import json
import re
import subprocess


def configure(lab, logs, runtime, *, max_bytes=1024**3):
    if type(max_bytes) is not int or not 1 <= max_bytes <= 2**50:
        raise ValueError("audit_max_bytes must be a positive integer at most 2**50")
    result = subprocess.run([str(lab / 'scripts/gvisor-host.sh'), runtime, 'trace', 'metadata'],
                            capture_output=True, text=True, timeout=30, check=True)
    if 'Name: audit' not in result.stdout:
        raise RuntimeError('runtime lacks the protected audit sink')
    points = []
    for line in result.stdout.splitlines():
        match = re.fullmatch(r'Name: (.*), optional fields: \[(.*)\], context fields: \[(.*)\]', line)
        if not match:
            continue
        name, optional, context = match.groups()
        # Completed syscalls, process lifecycle, mappings and successful execs.
        # Raw syscall records cover calls lacking a rich SecCheck schema.
        if not (name.endswith('/exit') or name.startswith('sentry/') or name == 'container/start'):
            continue
        points.append({'name': name,
                       'optional_fields': [x for x in optional.split('|') if x == 'fd_path'],
                       'context_fields': [x for x in context.split('|') if x]})
    if not points or not any(x['name'] == 'sentry/execve' for x in points):
        raise RuntimeError('runtime audit coverage is missing required lifecycle events')
    config = {'fail_on_error': True, 'trace_session': {'name': 'Default', 'points': points, 'ignore_missing': False,
              'sinks': [{'name': 'audit', 'config': {'max_bytes': max_bytes, 'path': '/lab/' + str((logs / 'audit.jsonl').relative_to(lab))},
                         'ignore_setup_error': False}]}}
    path = logs / 'audit-config.json'
    path.write_text(json.dumps(config, indent=2) + '\n')
    path.chmod(0o600)
    return path
