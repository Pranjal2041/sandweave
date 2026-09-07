#!/usr/bin/env python3
"""Run an independent no-KVM guest and its unprivileged network helpers."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import re
import socket
import sys
import time
import threading
import shutil
import cpu_broker
import runtime_store
import snapshot_store

started = phase = time.perf_counter()
started_at = time.time()
timings = {'started_at': started_at}


def mark(name):
    global phase
    now = time.perf_counter()
    timings[name] = now - phase
    phase = now

lab = Path(__file__).resolve().parent.parent
local = Path((lab / 'runs/local-path.txt').read_text().strip())
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--detach', action='store_true', help='keep a desktop running independently of the terminal/chat command')
parser.add_argument('--cpus', default=','.join(map(str, sorted(os.sched_getaffinity(0)))),
                    help='shared eligible CPU pool; defaults to the inherited Slurm allocation')
parser.add_argument('--network-policy', choices=['internet', 'offline'], default='internet')
parser.add_argument('--allow-cidr', action='append', default=[], help='explicit additional IPv4 egress network; host addresses remain blocked')
parser.add_argument('--guest-cpus', type=int, default=4, help='guest execution parallelism, independent of the shared host CPU pool')
parser.add_argument('--memory-mib', type=int, default=8192, help='guest page budget including anonymous memory and writable filesystem data; excludes runtime overhead')
parser.add_argument('--runtime-memory-mib', type=int, default=1024, help='separate sampled Go-runtime memory guard; permits transient overshoot')
parser.add_argument('--host-nice', type=int, default=0, help='experimental host per-thread nice value (0..19), not a whole-environment CPU weight')
parser.add_argument('--cpu-policy', choices=['shared', 'weighted', 'quota'], default='weighted')
parser.add_argument('--cpu-weight', type=int, default=100)
parser.add_argument('--cpu-quota', type=float, help='experimental average CPU equivalents, enforced by a sampled userspace controller')
parser.add_argument('--nftables', action='store_true')
parser.add_argument('--docker-data', action='store_true')
parser.add_argument('--docker-archive', type=Path, help='previously exported Docker state archive')
parser.add_argument('--guest-gs', action='store_true', help='preserve application GS; disable binary syscall patching')
parser.add_argument('--restore', type=Path, help='restore a complete lab snapshot using its recorded runtime and settings')
parser.add_argument('--verify', action='store_true', help='explicitly verify all snapshot/dependency hashes before restoring')
parser.add_argument('--cgroup', choices=['v1', 'v2'], default='v2')
parser.add_argument('name')
parser.add_argument('command', nargs=argparse.REMAINDER)
args = parser.parse_args()
saved_settings = None
snapshot_manifest = None
if args.verify and not args.restore:
    parser.error('--verify requires --restore')
if args.restore and not args.detach and (args.restore / 'snapshot-manifest.json').is_file():
    if args.verify:
        verification = snapshot_store.verify(lab, args.restore)
        if verification['status'] != 'passed':
            raise ValueError('snapshot verification failed: ' + verification['error'])
    snapshot_manifest = snapshot_store.inspect(lab, args.restore)
elif args.restore and not args.detach:
    parser.error('restore requires snapshot-manifest.json')
if args.restore and (args.restore / 'launch-settings.json').is_file():
    saved_settings = json.loads((args.restore / 'launch-settings.json').read_text())
    for key, value in saved_settings['settings'].items():
        option = '--' + key.replace('_', '-')
        if not any(x == option or x.startswith(option + '=') for x in sys.argv[1:]):
            setattr(args, key, value)
if args.runtime_memory_mib < 32:
    parser.error('runtime-memory-mib must be at least 32')
if args.guest_cpus < 1 or args.memory_mib < 64 or not 0 <= args.host_nice <= 19:
    parser.error('guest-cpus must be positive, memory-mib at least 64, and host-nice in 0..19')
if args.cpu_weight <= 0 or (args.cpu_policy == 'quota' and (args.cpu_quota is None or args.cpu_quota <= 0)):
    parser.error('cpu-weight must be positive and quota policy requires a positive cpu-quota')
if not re.fullmatch(r'[a-zA-Z0-9_-]+', args.name):
    parser.error('name must contain only letters, digits, dash or underscore')
if (local / 'gvisor/bundles' / args.name).exists():
    parser.error('sandbox name already has a bundle; choose a fresh name')
if args.detach:
    logs = lab / 'runs/gvisor' / args.name
    logs.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    command.remove('--detach')
    with (logs / 'launcher.out').open('wb') as output:
        child = subprocess.Popen(command, cwd=lab, stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT,
                                 start_new_session=True)
    (logs / 'launcher-pid.txt').write_text(str(child.pid) + '\n')
    print(f'Starting {args.name}, launcher PID {child.pid}; logs: {logs}; ports: {logs / "ports.json"}', flush=True)
    raise SystemExit(0)
mark('snapshot_metadata_seconds')
docker_archive = (args.docker_archive or local / 'gvisor/docker-state.tar').resolve()
if args.docker_archive and not args.docker_data:
    parser.error('--docker-archive requires --docker-data')
if args.docker_data:
    if not docker_archive.is_file():
        parser.error('Docker archive does not exist')
    if docker_archive.is_relative_to(local):
        docker_archive_arg = '/local/' + str(docker_archive.relative_to(local))
    elif docker_archive.is_relative_to(lab):
        docker_archive_arg = '/lab/' + str(docker_archive.relative_to(lab))
    else:
        parser.error('Docker archive must be under the lab or its local storage')
bundle_flags = ['--docker-data'] if args.docker_data else []
subprocess.run([sys.executable, str(lab / 'scripts/make-gvisor-bundle.py'), *bundle_flags, args.name, *args.command], check=True)
bundle = local / 'gvisor/bundles' / args.name
runtime = saved_settings['runtime'] if saved_settings else json.loads((lab / 'tools/gvisor-socket/runtime.json').read_text())
runtime_root = runtime_store.validate(lab, runtime, verify=not bool(args.restore))
runtime_arg = '/lab/' + str(runtime_root.relative_to(lab)) + '/runsc'
settings = {key: getattr(args, key) for key in ('guest_cpus', 'memory_mib', 'runtime_memory_mib', 'nftables', 'guest_gs', 'cgroup', 'network_policy', 'allow_cidr', 'cpu_policy', 'cpu_weight', 'cpu_quota', 'host_nice')}
launch_settings = {'settings': settings, 'runtime': runtime}
if snapshot_manifest is not None:
    launch_settings['base_image'] = snapshot_manifest['base_image']
(bundle / 'launch-settings.json').write_text(json.dumps(launch_settings, indent=2) + '\n')
mark('bundle_runtime_seconds')
if args.restore:
    saved_spec = args.restore / 'lab-spec.json'
    if not saved_spec.is_file():
        parser.error('checkpoint needs lab-spec.json copied from its original bundle config.json')
    (bundle / 'config.json').write_bytes(saved_spec.read_bytes())
    if (args.restore / 'fixtures.tar').exists():
        (bundle / 'fixtures.tar').write_bytes((args.restore / 'fixtures.tar').read_bytes())
spec = json.loads((bundle / 'config.json').read_text())
if args.restore:
    base = lab / snapshot_manifest['base_image']['path']
    # Older lab snapshots used node-local aliases. Materialize those immutable
    # inputs without changing OCI annotations or replacing different contents.
    for key, source in [('dev.gvisor.spec.rootfs.source', base),
                        ('dev.gvisor.tar.rootfs.upper', args.restore / 'fixtures.tar')]:
        saved_path = spec['annotations'][key]
        if saved_path.startswith('/local/'):
            alias = local / saved_path.removeprefix('/local/')
        elif saved_path.startswith('/lab/'):
            alias = lab / saved_path.removeprefix('/lab/')
        else:
            raise ValueError('unsupported snapshot input path')
        if alias.exists():
            if not os.path.samefile(alias, source):
                if alias.stat().st_size != source.stat().st_size:
                    raise ValueError('snapshot input size conflicts with existing file: ' + str(alias))
                if args.verify and runtime_store.digest(alias) != runtime_store.digest(source):
                    raise ValueError('snapshot input conflicts with existing file: ' + str(alias))
        else:
            alias.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, alias)
elif not args.restore:
    fixture_hash = runtime_store.digest(bundle / 'fixtures.tar')
    fixture_object = lab / 'images/fixtures' / (fixture_hash + '.tar')
    fixture_object.parent.mkdir(exist_ok=True)
    if not fixture_object.exists():
        shutil.copy2(bundle / 'fixtures.tar', fixture_object)
    spec['annotations']['dev.gvisor.tar.rootfs.upper'] = '/lab/' + str(fixture_object.relative_to(lab))
    spec['annotations']['dev.gvisor.spec.rootfs.source'] = '/lab/images/gvisor-ubuntu-ready-ae303ca.erofs'
spec['linux']['resources']['cpu'] = {}
spec['linux']['resources']['memory']['limit'] = args.memory_mib * 1024**2
(bundle / 'config.json').write_text(json.dumps(spec, indent=2) + '\n')
mark('restore_inputs_seconds')
logs = lab / 'runs/gvisor' / args.name
logs.mkdir(parents=True, exist_ok=True)
netdir = local / 'gvisor/network' / args.name
netdir.mkdir(parents=True, exist_ok=True, mode=0o700)
if any(netdir.glob('*.sock')):
    raise SystemExit(f'Refusing to replace existing sockets in {netdir}')
passt_socket, ethernet_socket = netdir / 'passt.sock', netdir / 'ethernet.sock'
inside_net = '/local/' + str(netdir.relative_to(local))
(bundle / 'network.json').write_text(json.dumps({
    'socket': inside_net + '/ethernet.sock', 'address': '10.0.2.15/24',
    'gateway': '10.0.2.2', 'mac': '02:00:00:00:00:15', 'mtu': 1500,
}, indent=2) + '\n')
children, files = [], []
registration = None
watch_done = threading.Event()
reservations = []
ports = {}
for guest_port in (80, 8080, 8000, 5901, 22):
    reservation = socket.socket()
    reservation.bind(('127.0.0.1', 0))
    reservations.append(reservation)
    ports[str(guest_port)] = reservation.getsockname()[1]
(logs / 'ports.json').write_text(json.dumps(ports, indent=2) + '\n')
host_interfaces = json.loads(subprocess.check_output(['ip', '-j', 'address'], text=True))
policy = {'mode': args.network_policy, 'guest': '10.0.2.15', 'gateway': '10.0.2.2',
          'dns': '10.0.2.3', 'forwarded_tcp_ports': list(map(int, ports)),
          'host_addresses': [address['local'] for interface in host_interfaces
                             for address in interface['addr_info'] if address['family'] == 'inet'],
          'allow_cidrs': args.allow_cidr}
(bundle / 'network-policy.json').write_text(json.dumps(policy, indent=2) + '\n')
mark('network_setup_seconds')


def spawn(command, logfile):
    output = logfile.open('wb')
    files.append(output)
    child = subprocess.Popen(['prlimit', '--as=536870912', '--', *command], cwd=lab, stdout=output, stderr=subprocess.STDOUT,
                             start_new_session=True)
    children.append(child)
    return child


def wait_socket(path, child):
    deadline = time.monotonic() + 15
    while not path.exists():
        if child.poll() is not None:
            raise RuntimeError(f'helper exited with {child.returncode}; see {logs}')
        if time.monotonic() > deadline:
            raise TimeoutError(str(path))
        time.sleep(.05)


try:
    # Apply eligibility to the launcher before spawning, so every runtime and
    # transport helper inherits the same shared CPU pool.
    selected_cpus = set()
    for part in args.cpus.split(','):
        bounds = list(map(int, part.split('-')))
        selected_cpus.update(range(bounds[0], bounds[-1] + 1))
    if not selected_cpus or not selected_cpus <= os.sched_getaffinity(0):
        raise ValueError('CPU pool must be a nonempty subset of the inherited allocation')
    os.sched_setaffinity(0, selected_cpus)
    if args.host_nice:
        os.nice(args.host_nice)
    if args.cpu_policy != 'shared':
        registration = cpu_broker.register(local, args.name, selected_cpus, args.cpu_weight,
                                             args.cpu_quota if args.cpu_policy == 'quota' else None)
        def watch_broker():
            while not watch_done.wait(1):
                status = registration.parent / 'status.json'
                try:
                    stale = time.time() - json.loads(status.read_text())['time'] > 3
                except (OSError, ValueError):
                    stale = True
                control_failed = registration.with_name(registration.name + '.failed')
                if stale or control_failed.exists():
                    # Leave no stopped processes behind if the controller dies.
                    # A failed controller invalidates the resource experiment.
                    reason = control_failed.read_text() if control_failed.exists() else 'CPU controller heartbeat expired\n'
                    (logs / 'cpu-controller-failed.txt').write_text(reason)
                    cpu_broker.resume_tree(os.getpid(), local / 'gvisor/state' / ('runsc-' + args.name + '.sock'))
                    cpu_broker.terminate_trees([child.pid for child in children if child.poll() is None])
                    return
        threading.Thread(target=watch_broker, daemon=True).start()
    forwards = []
    for guest_port, host_port in ports.items():
        forwards.extend(['-t', f'127.0.0.1/{host_port}:{guest_port}'])
    for reservation in reservations:
        reservation.close()
    passt = spawn(['passt', '-f', '-1', '-4', '-s', str(passt_socket),
                   '-a', '10.0.2.15', '-n', '24', '-g', '10.0.2.2', '-m', '1500',
                   '--dns-forward', '10.0.2.3', *forwards], logs / 'passt.log')
    wait_socket(passt_socket, passt)
    relay = spawn([sys.executable, str(lab / 'scripts/ethernet-relay.py'),
                   '--listen', str(ethernet_socket), '--passt', str(passt_socket),
                   '--policy', str(bundle / 'network-policy.json')], logs / 'relay.log')
    wait_socket(ethernet_socket, relay)
    mark('helpers_seconds')
    command = ['taskset', '-c', args.cpus, str(lab / 'scripts/gvisor-host.sh'),
               runtime_arg, '--platform=systrap', '--directfs=false',
               '--network=sandbox', f'--network-socket-config=/local/gvisor/bundles/{args.name}/network.json',
               '--ignore-cgroups', '--allow-suid', '--allow-rootfs-tar-annotation',
               f'--runtime-memory-limit={args.runtime_memory_mib * 1024**2}', f'--application-cpus={args.guest_cpus}', f'--app-memory-limit={args.memory_mib * 1024**2}',
               f'--in-sandbox-cgroup={args.cgroup}', '--net-raw', '--allow-packet-socket-write',
               '--sidecar-usage-policy=STRICT', '--root=/local/gvisor/state',
               '--debug', f'--debug-log=/lab/runs/gvisor/{args.name}/%COMMAND%.log']
    if args.nftables:
        command.append('--TESTONLY-nftables')
    if args.guest_gs:
        command.append('--systrap-disable-syscall-patching')
    if args.restore:
        checkpoint = snapshot_store.restore_path(local, args.restore, snapshot_manifest)
        timings['snapshot_storage'] = str(checkpoint)
        if checkpoint.is_relative_to(local):
            checkpoint_arg = '/local/' + str(checkpoint.relative_to(local))
        elif checkpoint.is_relative_to(lab):
            checkpoint_arg = '/lab/' + str(checkpoint.relative_to(lab))
        else:
            parser.error('checkpoint must be under the lab or its local storage')
        command += ['restore', '--image-path=' + checkpoint_arg]
    else:
        command += ['run']
    if args.docker_data:
        if not args.restore:
            command += ['--pass-fd=3:3']
            command[4:4] = ['sh', '-c', 'exec 3<"$1"; shift; exec "$@"', 'sh', docker_archive_arg]
    command += [f'--bundle=/local/gvisor/bundles/{args.name}', args.name]
    (logs / 'launch.json').write_text(json.dumps(command, indent=2) + '\n')
    print(f'RUNNING {args.name}; logs: {logs}', flush=True)
    # Use a pipe so re-opened guest /dev/stdout cannot reset a regular-file offset.
    with (logs / 'guest.out').open('wb') as output:
        mark('runtime_command_seconds')
        timings['setup_seconds'] = time.perf_counter() - started
        timings['runtime_spawned_at'] = time.time()
        snapshot_store.write_json(logs / 'restore-timings.json', timings)
        guest = subprocess.Popen(command, cwd=lab, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 start_new_session=True)
        children.append(guest)
        for line in guest.stdout:
            output.write(line)
            output.flush()
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
        result = guest.wait()
    (logs / 'exit-code.txt').write_text(str(result) + '\n')
    raise SystemExit(result)
finally:
    watch_done.set()
    if registration is not None:
        registration.unlink(missing_ok=True)
        cpu_broker.resume_tree(os.getpid(), local / 'gvisor/state' / ('runsc-' + args.name + '.sock'))
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    for output in files:
        output.close()
    for path in (ethernet_socket, passt_socket):
        path.unlink(missing_ok=True)
