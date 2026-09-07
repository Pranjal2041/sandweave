"""Opt-in, cooperative CUDA MPS partitions for the standalone Slurm lab."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import gvisor_gpu

QUALIFIED_DRIVER = '610.43.02'
INHERITED_CPUS = sorted(os.sched_getaffinity(0))


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return {'pid': pid, 'starttime': fields[19]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(record):
    return record is not None and identity(record['pid']) == record


def stop(record):
    if not alive(record):
        return
    try:
        os.kill(record['pid'], signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(30):
        if not alive(record):
            return
        time.sleep(.1)
    if alive(record):
        try:
            os.kill(record['pid'], signal.SIGKILL)
        except ProcessLookupError:
            pass


def environment(root, uuid):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('CUDA_', 'NVIDIA_', 'GENERAL_VM_MPS_'))
           and key not in ('LD_PRELOAD', 'LD_LIBRARY_PATH')}
    return {**env, 'CUDA_VISIBLE_DEVICES': uuid,
            'CUDA_MPS_PIPE_DIRECTORY': str(root / 'pipe'),
            'CUDA_MPS_LOG_DIRECTORY': str(root / 'logs')}


def control(root, uuid, command):
    result = subprocess.run(['nvidia-cuda-mps-control'], input=command + '\n',
                            text=True, capture_output=True, timeout=10,
                            env=environment(root, uuid))
    output = result.stdout.strip()
    if result.returncode or re.search(r'\b(error|failed|invalid|not found)\b', output, re.I):
        raise RuntimeError(f'MPS {command!r}: {output} {result.stderr}')
    return output


def validate(index, chunks, memory_mib):
    if chunks is None:
        if memory_mib is not None:
            raise ValueError('experimental GPU client memory requires an MPS partition')
        return
    if index is None or not 1 <= chunks <= 1024:
        raise ValueError('experimental GPU SM chunks requires --gpu and a value in 1..1024')
    if memory_mib is not None and memory_mib < 512:
        raise ValueError('experimental GPU client memory must be at least 512 MiB (context overhead counts)')
    if gvisor_gpu.driver_version() != QUALIFIED_DRIVER:
        raise ValueError(f'experimental MPS transport is qualified only with driver {QUALIFIED_DRIVER}')


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = root.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('MPS state directory must be private to the current host user')
    with (root / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def observer(lab):
    source = lab / 'scripts/mps-rm-observer.c'
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    library = lab / 'tools/gpu' / f'mps-rm-observer-{digest}.so'
    if not library.exists():
        temporary = library.with_suffix(f'.{os.getpid()}.tmp')
        try:
            subprocess.run(['gcc', '-shared', '-fPIC', '-O2', '-Wall', '-Wextra', '-Werror',
                            str(source), '-ldl', '-o', str(temporary)], check=True)
            temporary.replace(library)
        finally:
            temporary.unlink(missing_ok=True)
    return library


def shutdown(root, state):
    # Only the controller/server identities recorded by this private service.
    children = []
    if alive(state.get('controller')):
        pid = state['controller']['pid']
        try:
            for child in Path(f'/proc/{pid}/task/{pid}/children').read_text().split():
                command = Path(f'/proc/{child}/cmdline').read_bytes().split(b'\0')[0]
                if command.rsplit(b'/', 1)[-1] == b'nvidia-cuda-mps-server':
                    children.append(identity(int(child)))
        except (FileNotFoundError, ProcessLookupError):
            pass
    try:
        if alive(state.get('controller')):
            control(root, state['uuid'], 'quit')
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        pass
    stop(state.get('server'))
    for child in children:
        stop(child)
    stop(state.get('controller'))
    (root / 'service.json').unlink(missing_ok=True)


def start(root, lab, uuid):
    for directory in (root / 'pipe', root / 'logs'):
        directory.mkdir(exist_ok=True, mode=0o700)
    # Called only with no live leases and after stopping the recorded service.
    for path in (root / 'pipe').iterdir():
        if path.is_file() or path.is_socket():
            path.unlink()
    handles = root / 'handles.jsonl'
    handles.unlink(missing_ok=True)
    env = environment(root, uuid)
    env.update(LD_PRELOAD=str(observer(lab)), GENERAL_VM_MPS_RM_LOG=str(handles))
    # Reparent the shared service before registering this environment with the
    # CPU broker. Its CPU accounting must not belong to the first environment.
    controller = json.loads(subprocess.check_output([
        sys.executable, str(Path(__file__).resolve()), '--start-controller', str(root),
        '--cpus', ','.join(map(str, INHERITED_CPUS))], env=env, text=True, timeout=5))
    state = {'uuid': uuid, 'driver': QUALIFIED_DRIVER, 'controller': controller,
             'service_cpu_pool': INHERITED_CPUS,
             'server': None, 'handles': [], 'leases': {}}
    write_json(root / 'service.json', state)
    try:
        deadline = time.monotonic() + 10
        while not (root / 'pipe/control').exists():
            if not alive(controller) or time.monotonic() > deadline:
                raise RuntimeError(f'MPS controller did not start; see {root / "controller.out"}')
            time.sleep(.05)
        control(root, uuid, f'start_server -uid {os.getuid()}')
        servers = control(root, uuid, 'get_server_list').splitlines()
        if len(servers) != 1 or not servers[0].isdecimal():
            raise RuntimeError('expected exactly one owned MPS server: ' + repr(servers))
        state['server'] = identity(int(servers[0]))
        write_json(root / 'service.json', state)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if alive(state['server']) and control(root, uuid, f'get_server_status {servers[0]}') == 'ACTIVE':
                    records = [json.loads(line) for line in handles.read_text().splitlines()]
                    values = sorted({record['handle'] for record in records
                                     if record['pid'] == state['server']['pid']})
                    if not values or len(values) > 16 or not all(0 < value < 2**32 for value in values):
                        raise RuntimeError('invalid MPS server RM handle set')
                    state['handles'] = values
                    write_json(root / 'service.json', state)
                    return state
            except (OSError, ValueError):
                pass
            time.sleep(.1)
        raise RuntimeError('could not discover the active MPS server RM handles')
    except BaseException:
        shutdown(root, state)
        raise


def remove(root, state, name):
    lease = state['leases'][name]
    stop(lease.get('gateway_process'))
    if alive(state['server']):
        control(root, state['uuid'], 'sm_partition rm ' + lease['partition'])
    for path in Path(lease['gateway']).iterdir():
        path.unlink()
    Path(lease['gateway']).rmdir()
    del state['leases'][name]


class Lease:
    def __init__(self, root, name, state):
        self.root, self.name = root, name
        self.controller, self.server = state['controller'], state['server']
        self.handles = state['handles']
        self.metadata = {**state['leases'][name], 'experimental': True,
                         'controller': self.controller, 'server': self.server,
                         'trusted_rm_handles': self.handles, 'driver': state['driver']}

    def healthy(self):
        return all(alive(record) for record in
                   (self.controller, self.server, self.metadata['gateway_process']))

    def configure(self, spec, local):
        gateway = Path(self.metadata['gateway'])
        guest_env = {'CUDA_MPS_PIPE_DIRECTORY': '/opt/engine-mps',
                     'CUDA_MPS_SM_PARTITION': self.metadata['partition']}
        if self.metadata['client_memory_mib'] is not None:
            uuid = self.metadata['partition'].split('/')[0]
            guest_env['CUDA_MPS_PINNED_DEVICE_MEM_LIMIT'] = f'{uuid}={self.metadata["client_memory_mib"]}M'
        (gateway / 'environment').write_text(''.join(f'{key}={value}\n' for key, value in guest_env.items()))
        (gateway / 'environment').chmod(0o644)
        spec['mounts'].append({'destination': '/opt/engine-mps', 'type': 'bind',
                               'source': '/local/' + str(gateway.relative_to(local)),
                               'options': ['bind', 'ro']})
        spec['process']['env'] += [f'{key}={value}' for key, value in guest_env.items()]
        spec['process']['env'] = [value + ',profiling' if value.startswith('NVIDIA_DRIVER_CAPABILITIES=')
                                  else value for value in spec['process']['env']]

    def flags(self):
        return ['--host-uds=open', '--nvproxy-mps-clients=' + ','.join(map(str, self.handles))]

    def close(self):
        with locked(self.root):
            state = json.loads((self.root / 'service.json').read_text())
            if state['controller'] != self.controller:
                raise RuntimeError('MPS controller identity changed during the lease')
            try:
                remove(self.root, state, self.name)
            finally:
                write_json(self.root / 'service.json', state)
                if not state['leases']:
                    shutdown(self.root, state)


def acquire(lab, local, index, name, chunks, memory_mib):
    validate(index, chunks, memory_mib)
    uuid = gvisor_gpu.device_identity(index)['uuid']
    root = local / 'gvisor' / f'mps-{index}'
    with locked(root):
        state_path = root / 'service.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else None
        if state:
            if state['uuid'] != uuid:
                raise RuntimeError('MPS state belongs to a different GPU')
            for stale_name, lease in list(state['leases'].items()):
                if not alive(lease['owner']):
                    # Do not free resources while an orphaned sandbox may use them.
                    state_socket = local / 'gvisor/state' / f'runsc-{stale_name}.sock'
                    if state_socket.exists():
                        raise RuntimeError(f'orphaned MPS guest {stale_name}: stop it before reusing the service')
                    remove(root, state, stale_name)
            write_json(state_path, state)
            if not state['leases']:
                shutdown(root, state)
                state = None
            elif not alive(state['controller']) or not alive(state['server']):
                raise RuntimeError('MPS service failed; existing experimental guests must exit first')
        if state is None:
            state = start(root, lab, uuid)
        if name in state['leases']:
            raise ValueError('MPS environment name already leased')
        gateway = root / ('c-' + hashlib.sha256(name.encode()).hexdigest()[:12])
        partition = None
        process = None
        try:
            output = control(root, uuid, f'sm_partition add {uuid} {chunks}')
            match = re.fullmatch(r'Partition (GPU-[^\s/]+/[A-Za-z0-9+/=]+) created', output)
            if not match or not match[1].startswith(uuid + '/'):
                raise RuntimeError('MPS did not create the requested partition: ' + output)
            partition = match[1]
            gateway.mkdir(mode=0o755)
            gateway.chmod(0o755)
            with (root / f'{name}.gateway.log').open('ab') as output_file:
                process = subprocess.Popen([sys.executable, str(lab / 'scripts/mps_client_gateway.py'),
                                            str(gateway / 'control'), str(root / 'pipe/control')],
                                           stdout=output_file, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 5
            while not (gateway / 'control').exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('MPS client gateway failed to start')
                time.sleep(.05)
            state['leases'][name] = {'owner': identity(os.getpid()), 'partition': partition,
                                     'chunks': chunks, 'client_memory_mib': memory_mib,
                                     'gateway': str(gateway), 'gateway_process': identity(process.pid)}
            write_json(state_path, state)
            return Lease(root, name, state)
        except BaseException:
            if process:
                stop(identity(process.pid))
            if partition:
                control(root, uuid, 'sm_partition rm ' + partition)
            if gateway.exists():
                for path in gateway.iterdir():
                    path.unlink()
                gateway.rmdir()
            if not state['leases']:
                shutdown(root, state)
            raise


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Internal private MPS controller bootstrap')
    parser.add_argument('--start-controller', type=Path, required=True)
    parser.add_argument('--cpus', required=True)
    arguments = parser.parse_args()
    pid = os.fork()
    if pid:
        print(json.dumps(identity(pid)), flush=True)
    else:
        os.setsid()
        os.sched_setaffinity(0, set(map(int, arguments.cpus.split(','))))
        with open(os.devnull, 'rb') as input_file, (arguments.start_controller / 'controller.out').open('ab') as output:
            os.dup2(input_file.fileno(), 0)
            os.dup2(output.fileno(), 1)
            os.dup2(output.fileno(), 2)
        os.execvp('nvidia-cuda-mps-control', ['nvidia-cuda-mps-control', '-f', '-S', '-q'])
