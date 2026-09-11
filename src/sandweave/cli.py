"""CLI commands call the same public SDK and preserve guest stdout/stderr/exit status."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import select
import termios
import tty
import sys
import subprocess
import threading
import time

from . import Sandbox, SandboxError, CommandTimeout, CPU, Memory, Network, ProxyPolicy, Mount, Slurm
from .sandbox.snapshots import SnapshotRef
from .sandbox.targets import connect
from .sandbox.workspace import home, atomic_json, locked


def output(value):
    def encode(item):
        if isinstance(item, bytes):
            return {'bytes': len(item)}
        if isinstance(item, SnapshotRef):
            return {k: getattr(item, k) for k in ('id', 'state', 'location', 'digest', 'source', 'template')}
        return str(item)
    print(json.dumps(value, default=encode, sort_keys=True, indent=2))


def creation_options(parser):
    parser.add_argument('--template')
    parser.add_argument('--image', help='Docker/OCI base image, for example docker://python:3.12-slim')
    parser.add_argument('--setup')
    parser.add_argument('--cache')
    parser.add_argument('--snapshot')
    parser.add_argument('--cache-key')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--runtime', default='gvisor')
    parser.add_argument('--target')
    parser.add_argument('--cpu', type=int)
    parser.add_argument('--cpu-weight', type=int)
    parser.add_argument('--cpu-quota', type=float)
    parser.add_argument('--memory')
    parser.add_argument('--runtime-memory')
    parser.add_argument('--disk-memory', help='additional disk-backed guest memory, such as 16GiB')
    parser.add_argument('--disk-path', help='independent worker directory for disk-backed memory')
    parser.add_argument('--mount', action='append', default=[], help='JSON Mount object; repeat for multiple mounts')
    parser.add_argument('--gpu', help='auto, none, or an allocated model name')
    parser.add_argument('--network', choices=('internet', 'offline'))
    parser.add_argument('--proxy-file', help='private JSON proxy URLs: a list or a mapping from regions to lists')
    parser.add_argument('--proxy-policy', choices=('random', 'round_robin', 'same_proxy', 'same_region'))
    parser.add_argument('--proxy-region', help='restrict the proxy policy to this supplied region label')
    parser.add_argument('--name')
    parser.add_argument('--ttl', type=float)
    parser.add_argument('--startup-timeout', type=float)
    parser.add_argument('--keep-on-error', action='store_true')
    parser.add_argument('--experimental-gpu-live', action='store_true')


def command_options(parser):
    parser.add_argument('--argv', action='store_true', help='execute literal arguments without a guest shell')
    parser.add_argument('--timeout', type=float)
    parser.add_argument('--max-output-bytes', type=int)
    parser.add_argument('--cwd')
    parser.add_argument('--user')
    parser.add_argument('--shell')
    parser.add_argument('--no-stdin', action='store_true')
    parser.add_argument('--pty', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)


def creation(args):
    keys = ('template', 'image', 'setup', 'cache', 'snapshot', 'cache_key', 'refresh', 'runtime',
            'target', 'cpu', 'memory', 'gpu', 'network', 'name', 'ttl', 'startup_timeout',
            'keep_on_error', 'experimental_gpu_live')
    options = {k: getattr(args, k) for k in keys if getattr(args, k, None) is not None}
    if options.get('gpu') in ('auto', 'none'):
        options['gpu'] = options['gpu'] == 'auto'
    if getattr(args, 'cpu_weight', None) is not None or getattr(args, 'cpu_quota', None) is not None:
        options['cpu'] = CPU(args.cpu if args.cpu is not None else 1,
                             args.cpu_weight if args.cpu_weight is not None else 100,
                             args.cpu_quota)
    if any(getattr(args, key, None) for key in ('runtime_memory', 'disk_memory', 'disk_path')):
        options['memory'] = Memory(args.memory or '1GiB', args.runtime_memory or '512MiB',
                                   getattr(args, 'disk_memory', None), getattr(args, 'disk_path', None))
    if getattr(args, 'mount', None):
        options['mounts'] = [Mount(**json.loads(value)) for value in args.mount]
    if getattr(args, 'proxy_file', None):
        policy = (ProxyPolicy(args.proxy_policy or 'random', region=args.proxy_region)
                  if args.proxy_policy or args.proxy_region else None)
        options['network'] = Network(mode=args.network or 'internet', proxy=json.loads(Path(args.proxy_file).read_text()), policy=policy)
    elif getattr(args, 'proxy_policy', None) or getattr(args, 'proxy_region', None):
        raise ValueError('--proxy-policy and --proxy-region require --proxy-file')
    return options


def execute(env, args):
    arguments = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not arguments or (not args.argv and len(arguments) != 1):
        raise ValueError('provide one quoted command string after --, or use --argv for literal arguments')
    options = dict(timeout=args.timeout, binary=True, cwd=args.cwd, user=args.user,
                   shell=args.shell, max_output_bytes=args.max_output_bytes, pty=args.pty)
    process = env.exec(argv=arguments, **options) if args.argv else env.exec(arguments[0], **options)
    errors = []
    finished = threading.Event()
    terminal_state = None
    if args.pty and sys.stdin.isatty():
        size = os.get_terminal_size(sys.stdin.fileno())
        if process.poll() is None:
            process.resize(size.lines, size.columns)
        terminal_state = termios.tcgetattr(sys.stdin.fileno())
        tty.setraw(sys.stdin.fileno())

    def feed():
        try:
            if args.no_stdin:
                return
            while not finished.is_set():
                if select.select([sys.stdin], [], [], .1)[0]:
                    data = os.read(sys.stdin.fileno(), 64*1024)
                    if not data:
                        break
                    process.stdin.write(data)
        except (BrokenPipeError, ValueError):
            pass
        except Exception as error:
            errors.append(error)
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, ValueError):
                pass

    def drain(source, destination):
        try:
            # read(size) waits to fill size; chunk/readline would delay binary
            # output. Read available chunks and check completion only at EOF.
            while True:
                data = source._chunk()
                if data:
                    destination.write(data); destination.flush()
                elif process.poll() is not None:
                    # Output may have arrived between the empty read and exit
                    # observation. Exit publication follows guest spool flush.
                    data = source._chunk()
                    if data:
                        destination.write(data); destination.flush()
                        continue
                    break
                else:
                    time.sleep(.01)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=feed), *[threading.Thread(target=drain, args=(source, destination)) for source, destination in
               ((process.stdout, sys.stdout.buffer), (process.stderr, sys.stderr.buffer))]]
    for thread in threads:
        thread.start()
    try:
        code = process.wait()
    except CommandTimeout:
        code = 124
    except KeyboardInterrupt:
        process.terminate()
        code = 130
    finally:
        finished.set()
        for thread in threads:
            thread.join()
        if terminal_state is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, terminal_state)
    if errors:
        raise errors[0]
    return code if code >= 0 else 128 - code


def parser():
    root = argparse.ArgumentParser(prog='sandweave', description='Sandboxes without host sudo or KVM')
    sub = root.add_subparsers(dest='operation', required=True)
    create = sub.add_parser('create'); creation_options(create)
    run = sub.add_parser('run'); creation_options(run); command_options(run)
    existing = sub.add_parser('exec'); existing.add_argument('--target'); existing.add_argument('id'); command_options(existing)
    shell = sub.add_parser('shell'); shell.add_argument('id'); shell.add_argument('--target')
    command_options(shell); shell.set_defaults(command=['/bin/bash -i'], pty=True)
    listing = sub.add_parser('list'); listing.add_argument('--target'); listing.add_argument('--all', action='store_true')
    for name in ('info', 'status', 'inspect', 'pause', 'resume', 'terminate', 'stop', 'snapshot'):
        p = sub.add_parser(name); p.add_argument('id'); p.add_argument('--target')
        if name in ('stop', 'snapshot'):
            p.add_argument('--state', default='auto' if name == 'stop' else 'memory',
                           choices=('auto', 'memory', 'filesystem'))
            p.add_argument('--experimental-gpu-live', action='store_true')
    setup = sub.add_parser('setup', help='Prepare this worker with guided checks and repairs')
    setup.add_argument('id', nargs='?', help='Existing sandbox ID for guest script setup')
    setup.add_argument('script', nargs='?', help='Guest setup script (requires ID)')
    setup.add_argument('--target', help='Target for guest script setup')
    setup.add_argument('--template', help='Workload to prepare; defaults to coding')
    setup.add_argument('--assets', help='Use an existing prepared runtime directory')
    setup.add_argument('--directory', help='Store downloads, runtime files, workers and caches in this directory')
    setup.add_argument('--game-archive', help='Official GunSpinning Linux ZIP, when installing it for the first time')
    setup.add_argument('--yes', action='store_true', help='Apply available setup repairs without prompting')
    setup.add_argument('--build', action='store_true', help='Build the runtime from source instead of using a release')
    doctor = sub.add_parser('doctor', help='Check this worker and interactively repair problems')
    doctor.add_argument('--template', help='Workload to check; defaults to the last setup selection')
    doctor.add_argument('--check', action='store_true', help='Report checks without prompts or repairs')
    doctor.add_argument('--json', action='store_true', help='Report checks as JSON without prompts or repairs')
    cache = sub.add_parser('cache').add_subparsers(dest='cache_operation', required=True)
    save = cache.add_parser('save'); save.add_argument('id'); save.add_argument('key'); save.add_argument('--target')
    save.add_argument('--state', default='filesystem', choices=('filesystem', 'memory'))
    save.add_argument('--experimental-gpu-live', action='store_true')
    for operation in ('inspect', 'verify'):
        p = cache.add_parser(operation); p.add_argument('reference'); p.add_argument('--target')
    files = sub.add_parser('files').add_subparsers(dest='file_operation', required=True)
    for operation in ('upload', 'download'):
        p = files.add_parser(operation); p.add_argument('id'); p.add_argument('source'); p.add_argument('destination'); p.add_argument('--target')
    for operation in ('list', 'stat'):
        p = files.add_parser(operation); p.add_argument('id'); p.add_argument('path'); p.add_argument('--target')
    desktop = sub.add_parser('desktop').add_subparsers(dest='desktop_operation', required=True)
    shot = desktop.add_parser('screenshot'); shot.add_argument('id'); shot.add_argument('--output', required=True); shot.add_argument('--target')
    step = desktop.add_parser('step'); step.add_argument('id'); step.add_argument('action', help='JSON action'); step.add_argument('--output', required=True); step.add_argument('--target')
    action = desktop.add_parser('action'); action.add_argument('id'); action.add_argument('--input', required=True, help='JSON action'); action.add_argument('--target')
    record = sub.add_parser('vr').add_subparsers(dest='vr_operation', required=True).add_parser('record')
    record.add_argument('id'); record.add_argument('--duration', type=float, required=True); record.add_argument('--output', required=True)
    record.add_argument('--fps', type=int, default=30); record.add_argument('--target')
    configure = sub.add_parser('configure'); configure.add_argument('--assets', required=True)
    pools = sub.add_parser('pool').add_subparsers(dest='pool_operation', required=True)
    p = pools.add_parser('create'); creation_options(p)
    p.add_argument('--size', type=int, default=1); p.add_argument('--warm', type=int, default=0)
    p.add_argument('--weight', type=float, default=1); p.add_argument('--priority', type=int, default=0)
    p.add_argument('--placement', choices=('spread', 'pack'), default='spread')
    p = pools.add_parser('update'); p.add_argument('name'); p.add_argument('--target', required=True)
    p.add_argument('--size', type=int); p.add_argument('--warm', type=int)
    p.add_argument('--weight', type=float); p.add_argument('--priority', type=int)
    p.add_argument('--placement', choices=('spread', 'pack'))
    for action in ('status', 'close', 'exec'):
        p = pools.add_parser(action); p.add_argument('name'); p.add_argument('--target')
        if action == 'exec':
            command_options(p)
    p = pools.add_parser('list'); p.add_argument('--target')
    targets = sub.add_parser('targets').add_subparsers(dest='target_operation', required=True)
    targets.add_parser('list')
    p = targets.add_parser('add'); p.add_argument('name'); p.add_argument('--config', required=True, help='JSON SSH or Slurm target')
    p = targets.add_parser('remove'); p.add_argument('name')
    slurm = sub.add_parser('slurm').add_subparsers(dest='slurm_operation', required=True)
    p = slurm.add_parser('acquire')
    p.add_argument('--gpu'); p.add_argument('--cpus', type=int, default=1); p.add_argument('--memory', default='4GiB')
    for name in ('partition', 'qos', 'account'):
        p.add_argument('--' + name)
    p.add_argument('--walltime', default='1h'); p.add_argument('--queue-timeout', type=float)
    for action in ('status', 'close'):
        p = slurm.add_parser(action); p.add_argument('job_id')
    from .weave.cli import configure
    configure(sub)
    return root


def main(argv=None):
    arguments = parser()
    args = arguments.parse_args(argv)
    try:
        op = args.operation
        if op in ('cluster', 'job', 'pool', 'dashboard'):
            from .weave.cli import main as weave_main
            result = weave_main(args)
            if result is not None:
                return result
            if op == 'pool' and (args.pool_operation == 'update' or
                    getattr(args, 'weight', 1) != 1 or getattr(args, 'priority', 0) != 0 or
                    getattr(args, 'placement', 'spread') != 'spread'):
                raise ValueError('pool scheduling options require a cluster target')
        if op == 'setup':
            if bool(args.id) != bool(args.script):
                raise ValueError('guest setup requires both ID and SCRIPT; use plain sandweave setup for this worker')
            if args.id and (args.template or args.assets or args.directory or args.game_archive or args.yes or args.build):
                raise ValueError('worker setup options cannot be used with guest ID and SCRIPT')
            if not args.id and args.target:
                raise ValueError('run sandweave setup on the worker; --target applies to guest script setup')
        if op == 'doctor' or (op == 'setup' and not args.id):
            from . import onboarding
            return onboarding.main(args)
        if op == 'targets':
            from .onboarding import configuration as read_configuration
            configuration = home() / 'config.json'
            if args.target_operation == 'list':
                output(read_configuration().get('targets', {}))
            else:
                with locked(configuration.with_suffix('.lock')):
                    config = read_configuration()
                    targets = config.setdefault('targets', {})
                    if not isinstance(targets, dict):
                        raise ValueError('Saved targets must be a JSON object')
                    if args.target_operation == 'add':
                        value = json.loads(args.config)
                        if not isinstance(value, dict) or not ('host' in value or 'job_id' in value):
                            raise ValueError('target config must declare host or job_id')
                        targets[args.name] = value
                    else:
                        targets.pop(args.name, None)
                    atomic_json(configuration, config)
            return 0
        if op == 'slurm':
            if args.slurm_operation == 'acquire':
                options = {k: v for k, v in vars(args).items() if k not in ('operation', 'slurm_operation') and v is not None}
                if options.get('gpu') == 'auto':
                    options['gpu'] = True
                allocation = Slurm.acquire(**options)
                record = {'job_id': allocation.job_id, 'config': allocation.config, 'owned': True,
                          'queue_seconds': allocation.queue_seconds}
                atomic_json(home() / 'allocations' / ('cli-' + allocation.job_id + '.json'), record)
                output(record)
            elif args.slurm_operation == 'status':
                output(Slurm.connect(args.job_id)._job())
            else:
                record = home() / 'allocations' / ('cli-' + args.job_id + '.json')
                if not record.is_file():
                    raise ValueError('CLI only closes allocations created by sandweave slurm acquire')
                saved = json.loads(record.read_text())
                allocation = Slurm(saved['job_id'], saved['config'], owned=saved['owned'])
                allocation._job()  # Verify user ownership before cancellation.
                allocation.close()
                atomic_json(record, {**saved, 'owned': False})
            return 0
        if op == 'pool':
            connection = None
            try:
                action = args.pool_operation
                name = getattr(args, 'name', None)
                if action == 'create' and not name:
                    raise ValueError('named pool creation requires --name')
                parameters = None
                recipe = None
                if action == 'create':
                    from .templates.resolve import Template, setup_step
                    from dataclasses import is_dataclass
                    options = creation(args)
                    options.pop('name', None); options.pop('target', None)
                    if not (options.get('cache') or options.get('snapshot')):
                        recipe = Template(options.pop('template', 'coding')).resolve()
                        setup = options.pop('setup', None)
                        if setup:
                            recipe['setup_steps'].append(setup_step(setup))
                        options['template'] = recipe
                    else:
                        connection = connect(args.target)
                        saved = connection.call('snapshot_spec', reference=options.get('cache') or options['snapshot'])
                        recipe = saved['spec']['template']
                        options.pop('snapshot', None)
                        options['cache'] = saved['reference']
                        connection.close()
                    options = {key: asdict(value) if is_dataclass(value) else value for key, value in options.items()}
                    if options.get('mounts'):
                        options['mounts'] = [asdict(value) for value in options['mounts']]
                    parameters = {'size': args.size, 'warm': args.warm, 'options': options}
                connection = connect(args.target, template=recipe)
                if action != 'exec':
                    output(connection.call('pool', action=action, name=name,
                                           arguments=parameters))
                    return 0
                lease = connection.call('pool', action='checkout', name=name)
                try:
                    with Sandbox.connect(lease['id'], target=args.target) as env:
                        return execute(env, args)
                finally:
                    connection.call('pool', action='release', name=name, lease_id=lease['lease_id'])
            finally:
                if connection is not None:
                    connection.close()
        if op == 'configure':
            from .onboarding import save_configuration
            path = Path(args.assets).expanduser().resolve()
            if not path.is_dir():
                raise FileNotFoundError(path)
            save_configuration(assets=str(path))
            return 0
        if op == 'create':
            # create deliberately outlives this short-lived CLI process.
            env = Sandbox(detached=True, **creation(args))
            print(env.id); env.close()
            return 0
        if op == 'run':
            with Sandbox(**creation(args)) as env:
                return execute(env, args)
        if op == 'list' or (op == 'cache' and args.cache_operation != 'save'):
            connection = connect(args.target)
            try:
                if op == 'list':
                    values = connection.call('list')
                    output([{k: value.get(k) for k in ('id', 'name', 'state', 'created_at')} for value in values
                            if args.all or value['state'] not in ('terminated', 'stopped', 'failed')])
                else:
                    value = connection.call('snapshot_info' if args.cache_operation == 'inspect' else 'snapshot_verify',
                                            reference=args.reference)
                    output(value)
                    if args.cache_operation == 'verify' and value['status'] != 'passed':
                        return 1
            finally:
                connection.close()
            return 0
        with Sandbox.connect(args.id, target=getattr(args, 'target', None)) as env:
            if op in ('exec', 'shell'):
                if op == 'shell' and not args.command:
                    args.command = ['/bin/bash -i']
                return execute(env, args)
            if op == 'info':
                output(env.info)
            elif op in ('status', 'inspect', 'pause', 'resume', 'terminate'):
                output(getattr(env, 'status' if op == 'inspect' else op)())
            elif op in ('stop', 'snapshot'):
                output(getattr(env, op)(state=args.state, experimental_gpu_live=args.experimental_gpu_live))
            elif op == 'setup':
                env.setup(args.script)
            elif op == 'cache':
                output(env.cache(args.key, state=args.state, experimental_gpu_live=args.experimental_gpu_live))
            elif op == 'files':
                if args.file_operation in ('list', 'stat'):
                    output(getattr(env.files, args.file_operation)(args.path))
                else:
                    getattr(env.files, args.file_operation)(args.source, args.destination)
            elif op == 'desktop':
                if args.desktop_operation == 'action':
                    output(env.desktop.action(json.loads(args.input)))
                    return 0
                image = env.desktop.screenshot() if args.desktop_operation == 'screenshot' else env.desktop.step(json.loads(args.action)).image
                image.save(args.output)
            elif op == 'vr':
                if args.duration <= 0:
                    raise ValueError('recording duration must be positive')
                with env.vr.record(args.output, fps=args.fps):
                    time.sleep(args.duration)
        return 0
    except KeyboardInterrupt:
        print('sandweave: cancelled', file=sys.stderr)
        return 130
    except (SandboxError, ValueError, OSError, TimeoutError, subprocess.SubprocessError) as error:
        print(f'sandweave: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
