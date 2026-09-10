"""Cluster, pool and job commands share the Python clients."""
import json
from pathlib import Path


def configure(sub):
    cluster = sub.add_parser('cluster', help='Manage workers and sandbox capacity').add_subparsers(dest='cluster_operation', required=True)
    p = cluster.add_parser('start', help='Start a durable controller and a local worker')
    p.add_argument('name', nargs='?', default='lab')
    p.add_argument('--directory')
    p.add_argument('--no-worker', action='store_true')
    p.add_argument('--slots', type=int)
    p.add_argument('--memory')
    p.add_argument('--cpus', type=int, help='Maximum eligible CPU cores for the local worker')
    p.add_argument('--gpus', type=int, help='Maximum eligible GPUs for the local worker; 0 disables GPUs')
    p.add_argument('--listen', help='Controller bind address, for example 0.0.0.0:8765')
    p.add_argument('--tls-cert'); p.add_argument('--tls-key'); p.add_argument('--token-file')
    p = cluster.add_parser('connect', help='Save a target for a controller on another machine')
    p.add_argument('name'); p.add_argument('address', nargs='?')
    p.add_argument('--host'); p.add_argument('--directory')
    p.add_argument('--token-file'); p.add_argument('--ca-file')
    for action in ('status', 'workers', 'stop', 'events'):
        p = cluster.add_parser(action); p.add_argument('name', nargs='?', default='lab')
        if action == 'events':
            p.add_argument('--after', type=int, default=0)
    for action in ('add', 'join'):
        p = cluster.add_parser(action, help='Register an existing worker' if action == 'add' else 'Join from this worker machine')
        p.add_argument('name', nargs='?', default='lab'); p.add_argument('--target', default='local')
        p.add_argument('--slots', type=int); p.add_argument('--memory')
        if action == 'join':
            p.add_argument('--cpus', type=int, help='Maximum eligible CPU cores; defaults to all available')
            p.add_argument('--gpus', type=int, help='Maximum eligible GPUs; defaults to all available')
            p.add_argument('--token-file'); p.add_argument('--ca-file')
        p.add_argument('--label', action='append', default=[], metavar='KEY=VALUE')
    for action in ('drain', 'resume', 'remove'):
        p = cluster.add_parser(action); p.add_argument('name'); p.add_argument('worker')
    p = cluster.add_parser('backup'); p.add_argument('name'); p.add_argument('destination')
    jobs = sub.add_parser('job', help='Submit and inspect durable commands').add_subparsers(dest='job_operation', required=True)
    from ..cli import creation_options
    p = jobs.add_parser('submit'); creation_options(p)
    p.add_argument('--pool'); p.add_argument('--file', action='append', default=[], metavar='DEST=SOURCE')
    p.add_argument('--items', help='JSON file containing a list of task inputs')
    p.add_argument('--retries', type=int, default=0)
    p.add_argument('--retry-code', type=int, action='append', default=[])
    p.add_argument('--retry-infrastructure', action='store_true')
    p.add_argument('--timeout', type=float); p.add_argument('--every', type=float)
    p.add_argument('command', nargs='+')
    for action in ('status', 'result', 'cancel'):
        p = jobs.add_parser(action); p.add_argument('id'); p.add_argument('--target', default='lab')


def main(args):
    from ..cli import output, creation, execute
    from .client import Cluster, cluster_config
    if args.operation == 'cluster':
        action = args.cluster_operation
        if action == 'start':
            cluster = Cluster.start(args.name, directory=args.directory, local_worker=not args.no_worker,
                                    slots=args.slots, memory=args.memory, cpus=args.cpus, gpus=args.gpus,
                                    listen=args.listen, tls_cert=args.tls_cert, tls_key=args.tls_key, token_file=args.token_file)
        elif action == 'connect':
            from .client import save_target
            from ..sandbox.targets import _ssh
            if args.address:
                if args.host or args.directory:
                    raise ValueError('provide an address or --host and --directory, not both')
                cluster = Cluster.connect(args.address, token_file=args.token_file, ca_file=args.ca_file)
                config = {**cluster.config, 'forward': True}
            else:
                if not args.host or not args.directory or not Path(args.directory).is_absolute():
                    raise ValueError('provide a controller URL, or --host with an absolute --directory')
                info = json.loads(_ssh(args.host, ['python3', '-c',
                    'import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())',
                    str(Path(args.directory) / 'controller.json')]))
                config = {'hostname': info['hostname'], 'ssh_host': args.host, 'directory': args.directory, 'forward': True}
                cluster = Cluster(config, args.name)
            cluster.connection.call('ping')
            save_target(args.name, config)
            cluster.close()
            output({'name': args.name, 'address': args.address or 'ssh://' + args.host + args.directory,
                    'connected': True})
            return 0
        else:
            cluster = Cluster.connect(args.name, token_file=getattr(args, 'token_file', None),
                                      ca_file=getattr(args, 'ca_file', None))
        try:
            if action in ('start', 'status'):
                output(cluster.info)
            elif action == 'workers':
                output(cluster.workers)
            elif action == 'stop':
                output(cluster.stop())
            elif action == 'events':
                output(cluster.events(after=args.after))
            elif action == 'backup':
                output({'path': cluster.backup(args.destination)})
            elif action in ('drain', 'resume', 'remove'):
                output(getattr(cluster, 'remove_worker' if action == 'remove' else action)(args.worker))
            elif action in ('add', 'join'):
                labels = {}
                for item in args.label:
                    key, separator, value = item.partition('=')
                    if not separator or not key:
                        raise ValueError('labels use KEY=VALUE')
                    labels[key] = value
                target = args.target
                if action == 'join':
                    if target != 'local':
                        raise ValueError('join starts a worker on this machine; use cluster add for remote targets')
                    from .worker import start
                    output(start(cluster.config, cpus=args.cpus, gpus=args.gpus,
                                 slots=args.slots, memory=args.memory, labels=labels))
                else:
                    output(cluster.add_worker(target, slots=args.slots, memory=args.memory, labels=labels))
        finally:
            cluster.close()
        return 0
    if args.operation == 'job':
        from .jobs import Job
        if args.job_operation == 'submit':
            options = creation(args)
            target = options.pop('target', None) or 'lab'
            if args.pool:
                # argparse supplies default flags which are not user overrides.
                for key, value in (('runtime', 'gvisor'), ('refresh', False), ('keep_on_error', False), ('experimental_gpu_live', False)):
                    if options.get(key) == value:
                        options.pop(key)
            commands = args.command[1:] if args.command[:1] == ['--'] else args.command
            if len(commands) != 1:
                raise ValueError('provide one quoted command string after --')
            files = {}
            for item in args.file:
                destination, separator, source = item.partition('=')
                if not separator:
                    raise ValueError('job files use DEST=SOURCE')
                files[destination] = source
            job = Job.submit(commands[0], target=target, pool=args.pool, detached=True,
                files=files, items=json.loads(Path(args.items).read_text()) if args.items else None,
                retries=args.retries, retry_codes=args.retry_code, retry_infrastructure=args.retry_infrastructure,
                timeout=args.timeout, every=args.every, **options)
            print(job.id)
            job.close()
            return 0
        job = Job.connect(args.id, target=args.target)
        try:
            if args.job_operation == 'status':
                output(job.info)
            elif args.job_operation == 'cancel':
                output(job.cancel())
            else:
                from dataclasses import asdict
                result = job.result()
                output([asdict(r) for r in result] if isinstance(result, list) else asdict(result))
        finally:
            job.close()
        return 0
    if args.operation == 'pool' and cluster_config(getattr(args, 'target', None)) is not None:
        from .pool import Pool
        if args.pool_operation == 'list':
            with Cluster.connect(args.target) as cluster:
                output(cluster.info['pools'])
            return 0
        if args.pool_operation == 'create':
            options = creation(args)
            if not options.get('name'):
                raise ValueError('named pool creation requires --name')
            pool = Pool(size=args.size, warm=args.warm, weight=args.weight, priority=args.priority,
                        placement=args.placement, detached=True, **options)
            pool.start()
            output(pool.info)
            pool.connection.close()
            return 0
        with Pool.connect(args.name, target=args.target) as pool:
            if args.pool_operation == 'status':
                output(pool.info)
            elif args.pool_operation == 'close':
                output(pool.terminate())
            elif args.pool_operation == 'update':
                output(pool.update(**{k: getattr(args, k) for k in ('size', 'warm', 'weight', 'priority', 'placement') if getattr(args, k) is not None}))
            elif args.pool_operation == 'exec':
                with pool.acquire() as env:
                    return execute(env, args)
        return 0
    return None
