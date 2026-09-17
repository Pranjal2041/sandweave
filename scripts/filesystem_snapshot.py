"""Cold snapshots of the root overlay and persistent tmpfs mounts."""
from pathlib import Path, PurePosixPath
import re
import subprocess
import json
import time

# These are recreated when the saved filesystem boots with new processes.
VOLATILE = ('/dev', '/proc', '/sys', '/run', '/tmp')


def inside(path, lab, local):
    path = Path(path).resolve()
    for root, prefix in ((local, '/local/'), (lab, '/lab/')):
        if path.is_relative_to(root):
            return prefix + str(path.relative_to(root))
    raise ValueError('snapshot must reside under the lab or node-local storage')


def unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def mount_table(mountinfo):
    result = {}
    for line in mountinfo.splitlines():
        fields, fs = line.split(' - ', 1)
        fields, fs = fields.split(), fs.split()
        result[fields[0]] = {'parent': fields[1], 'device': fields[2],
            'root': unescape(fields[3]), 'destination': unescape(fields[4]),
            'readonly': 'ro' in fields[5].split(','), 'type': fs[0], 'line': line}
    return result


def self_bind(mount, mounts):
    """A second view of the same backing directory adds no files to export."""
    parent = mounts.get(mount['parent'])
    if parent is None or (mount['device'], mount['type']) != (parent['device'], parent['type']):
        return False
    try:
        relative = PurePosixPath(mount['destination']).relative_to(parent['destination'])
    except ValueError:
        return False
    return PurePosixPath(mount['root']) == PurePosixPath(parent['root']) / relative


def inventory(spec, mountinfo):
    """Fail rather than silently omit unrecognized writable storage."""
    configured = {m['destination']: m for m in spec['mounts']}
    external = json.loads(spec.get('annotations', {}).get('dev.sandweave.external-mounts', '[]'))
    if any(m['snapshot'] != 'rebind' for m in external):
        raise ValueError('external mount policy rejects capture; explicitly choose rebind for shared external state')
    rebound = {m['destination'] for m in external}
    result = []
    seen = set()
    mounts = mount_table(mountinfo)
    for entry in mounts.values():
        root, destination = entry['root'], entry['destination']
        if destination == '/' or any(destination == p or destination.startswith(p + '/') for p in VOLATILE):
            continue
        if entry['readonly']:
            continue
        if destination in rebound:
            continue
        # Docker and other applications self-bind directories to change mount
        # propagation. Their files are already in the backing filesystem's
        # export. Compare both filesystem identity and the underlying path:
        # a bind from a different directory must not be mistaken for this.
        if self_bind(entry, mounts):
            continue
        # Docker's overlay mounts refer back to data captured in its storage
        # directory. Docker reconstructs these when containers start again.
        if entry['type'] == 'overlay' and root == '/' and destination.startswith('/var/lib/docker/'):
            continue
        mount = configured.get(destination)
        if mount is None or mount['type'] != 'tmpfs' or entry['type'] != 'tmpfs' or root != '/':
            raise ValueError('unsupported writable mount; snapshot would be incomplete: ' + entry['line'])
        if destination in seen:
            raise ValueError('stacked writable mounts are not qualified: ' + destination)
        seen.add(destination)
        result.append({'destination': destination, 'options': mount.get('options', []),
                       'file': f'mount-{len(result)}.tar'})
    expected = {m['destination'] for m in spec['mounts'] if m['type'] == 'tmpfs'
                and not any(m['destination'] == p or m['destination'].startswith(p + '/') for p in VOLATILE)}
    if expected != {m['destination'] for m in result}:
        raise ValueError('persistent mount layout differs from the launch spec')
    return result


def capture(command, name, dest, spec, lab, local):
    help_result = subprocess.run([*command, 'tar', 'rootfs-upper', '--help'],
                                 capture_output=True, text=True, timeout=30)
    if 'restore-mount' not in help_result.stdout + help_result.stderr:
        raise ValueError('filesystem capture requires the updated runtime; this existing environment has not been paused')
    state = json.loads(subprocess.check_output([*command, 'state', name], timeout=30, text=True))
    if state['status'] not in ('running', 'paused'):
        raise ValueError('filesystem snapshot requires a running or paused environment')
    resume = state['status'] == 'running'
    try:
        if resume:
            subprocess.run([*command, 'pause', name], check=True, timeout=30, capture_output=True)
        mountinfo = subprocess.check_output([*command, 'read', name, '/proc/1/mountinfo'], timeout=30, text=True)
        (dest / 'mountinfo.txt').write_text(mountinfo)
        mounts = inventory(spec, mountinfo)
        phases = {}
        for entry in [{'destination': '/', 'file': 'rootfs-upper.tar'}, *mounts]:
            tick = time.perf_counter()
            with (dest / (entry['file'] + '.log')).open('wb') as log:
                subprocess.run([*command, 'tar', 'rootfs-upper', '--path=' + entry['destination'],
                                '--file=' + inside(dest / entry['file'], lab, local), name],
                               check=True, stdout=log, stderr=subprocess.STDOUT, timeout=600)
            phases[entry['destination']] = time.perf_counter() - tick
        return {'mounts': mounts, 'recreated_on_boot': list(VOLATILE),
                'consistency': 'crash-consistent filesystem cut; application buffers in RAM are not saved',
                'export_seconds': phases}
    finally:
        if resume:
            subprocess.run([*command, 'resume', name], check=True, timeout=30, capture_output=True)


def prepare_boot(spec, command):
    # Waiting happens before systemd/apps start. The host installs persistent
    # mounts through the Sentry RPC, then releases PID 1 to exec the boot command.
    command = list(command)
    if command and command[0] == '--':
        command.pop(0)
    command = command or ['/sbin/init']
    spec['process']['args'] = ['/bin/sh', '-c',
        'touch /run/engine-fs-waiting; while [ ! -e /run/engine-fs-ready ]; do sleep .05; done; '
        'rm -f /run/engine-fs-waiting /run/engine-fs-ready; exec "$@"', 'fs-restore', *command]


def finish_boot(command, name, checkpoint, manifest, lab, local, guest, done):
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline and not done.is_set():
        if guest.poll() is not None:
            raise RuntimeError('filesystem restore runtime exited before mount setup')
        try:
            result = subprocess.run([*command, 'read', name, '/run/engine-fs-waiting'],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except subprocess.TimeoutExpired:
            # Rootfs import can hold the RPC server during a slow cold boot.
            # Retry this read-only check within the overall startup deadline.
            continue
        if result.returncode == 0:
            break
        time.sleep(.1)
    else:
        raise TimeoutError('filesystem restore boot staging did not become ready')
    for mount in manifest['filesystem']['mounts']:
        # The engine separates generic VFS flags from tmpfs-specific options.
        options = ','.join(mount['options'])
        subprocess.run([*command, 'tar', 'rootfs-upper', '--restore-mount', '--path=' + mount['destination'],
                        '--mount-data=' + options,
                        '--file=' + inside(checkpoint / mount['file'], lab, local), name],
                       check=True, timeout=600, capture_output=True)
    subprocess.run([*command, 'exec', name, 'touch', '/run/engine-fs-ready'], check=True, timeout=30, capture_output=True)
