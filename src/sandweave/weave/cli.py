"""Cluster, pool and job commands share the Python clients."""
import json
from pathlib import Path
import shlex


def start_options(args):
    """Transport names select listener defaults; explicit listeners still work."""
    listen = args.listen
    transport = args.transport
    if transport == 'ssh':
        if listen or args.tls_cert or args.tls_key:
            raise ValueError('--transport ssh uses a loopback listener; omit --listen and TLS options')
        listen = '127.0.0.1:0'
    elif transport in ('http', 'https'):
        if transport == 'http' and (args.tls_cert or args.tls_key):
            raise ValueError('Use --transport https with TLS certificates')
        if transport == 'https' and not (args.tls_cert and args.tls_key):
            raise ValueError('HTTPS needs --tls-cert and --tls-key for a certificate trusted by your workers')
        listen = listen or '0.0.0.0:8765'
    if args.advertise:
        validate_advertise(args.advertise)
    return listen


def validate_advertise(value):
    import ipaddress
    from urllib.parse import urlsplit
    from .transport import address
    public = address(value)
    if 'url' not in public or 'token' in public:
        raise ValueError('--advertise must be an HTTP or HTTPS address without credentials')
    hostname = urlsplit(public['url']).hostname
    try:
        unspecified = ipaddress.ip_address(hostname).is_unspecified
    except ValueError:
        unspecified = False
    if unspecified:
        raise ValueError('--advertise needs a reachable hostname or IP, not a wildcard bind address')
    return public['url']


def instructions(cluster, *, advertise=None):
    """Print executable commands using this controller's actual metadata."""
    import getpass
    import ipaddress
    from urllib.parse import urlsplit, quote
    from .client import metadata
    from .transport import join_link
    from ..sandbox.workspace import atomic_json
    if 'directory' not in cluster.config:
        raise ValueError('Run cluster instructions on the controller machine using its saved name')
    cluster.connection.call('ping')
    info = metadata(cluster.config)
    root = Path(cluster.config['directory'])
    display_file = root / 'connection-display.json'
    if advertise:
        advertise = validate_advertise(advertise)
        atomic_json(display_file, {'advertise': advertise})
    elif display_file.exists():
        advertise = validate_advertise(json.loads(display_file.read_text())['advertise'])
    hostname = info['hostname']
    ssh_host = cluster.config.get('ssh_host') or (getpass.getuser() + '@' + hostname)
    if cluster.config.get('ssh_port'):
        ssh_host += ':' + str(cluster.config['ssh_port'])
    ssh = 'ssh://' + ssh_host + quote(str(root), safe='/')
    url = info['address']
    parsed = urlsplit(url)
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == 'localhost'
    if advertise:
        url = advertise
        loopback = False  # An explicit address may be a reverse proxy or tunnel.
    link = join_link(url, info['token'])
    base, fragment = link.split('#', 1)
    scope = ' (this machine only)' if loopback else ''
    print('Cluster ' + str(cluster.name) + ' is running.')
    print('\nDashboard' + scope + ': ' + base + '/dashboard/#' + fragment)
    print(urlsplit(url).scheme.upper() + scope + ': ' + link)
    print('SSH: ' + ssh)
    print('\nJoin a worker (copy either command):' if not loopback else '\nJoin a worker through SSH:')
    if not loopback:
        print('  sandweave cluster join ' + shlex.quote(link))
    print('  sandweave cluster join ' + shlex.quote(ssh))
    print('\nDashboard through SSH (run on your browser\'s machine):')
    print('  sandweave dashboard ' + shlex.quote(ssh))
    print('\nWorkers use their available resources. Optional limits: --cpus 4 --gpus 0 --memory 8GiB')
    print('Links contain the cluster credential; share them privately.')
    if urlsplit(url).scheme == 'http' and not loopback:
        print('HTTP is unencrypted. SSH uses your existing SSH login.')
    print('State: ' + str(root))


def configure(sub):
    import argparse
    p = sub.add_parser('dashboard', help='Open the cluster monitoring dashboard')
    p.add_argument('target', nargs='?', default='lab')
    p.add_argument('--no-open', action='store_true', help='Print the sign-in link without opening a browser')
    p.add_argument('--token-file'); p.add_argument('--ca-file')
    cluster = sub.add_parser('cluster', help='Manage workers and sandbox capacity').add_subparsers(dest='cluster_operation', required=True)
    p = cluster.add_parser('start', help='Start a durable controller and a local worker')
    p.add_argument('name', nargs='?', default='lab')
    p.add_argument('--directory')
    p.add_argument('--no-worker', action='store_true')
    p.add_argument('--slots', type=int)
    p.add_argument('--memory')
    p.add_argument('--cpus', type=int, help='Maximum eligible CPU cores for the local worker')
    p.add_argument('--gpus', type=int, help='Maximum eligible GPUs for the local worker; 0 disables GPUs')
    p.add_argument('--listen', help='Bind address (default: all IPv4 interfaces, automatically assigned port)')
    p.add_argument('--transport', choices=('ssh', 'http', 'https'),
                   help=argparse.SUPPRESS)  # Compatibility with explicit 0.2.1 settings.
    p.add_argument('--advertise', help='Reachable HTTP(S) address to print instead of the listener address')
    p.add_argument('--json', action='store_true', help='Print machine-readable cluster status')
    p.add_argument('--tls-cert'); p.add_argument('--tls-key'); p.add_argument('--token-file')
    p.add_argument('--monitor-interval', type=float, help='Seconds between monitoring samples (default: 5)')
    p.add_argument('--history-hours', type=float, help='Measurement retention in hours (default: 24, maximum: 168)')
    p = cluster.add_parser('connect', help='Save a target for a controller on another machine')
    p.add_argument('name'); p.add_argument('address', nargs='?')
    p.add_argument('--host'); p.add_argument('--directory')
    p.add_argument('--token-file'); p.add_argument('--ca-file')
    p = cluster.add_parser('instructions', help='Print complete join and dashboard commands again')
    p.add_argument('name', nargs='?', default='lab')
    p.add_argument('--advertise', help='Reachable HTTP(S) address to print')
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
    if args.operation == 'dashboard':
        import time
        import socket
        import webbrowser
        with Cluster.connect(args.target, token_file=args.token_file, ca_file=args.ca_file) as cluster:
            url = cluster.dashboard()
            print(url, flush=True)
            print('This sign-in link expires in 60 seconds. The browser session lasts 8 hours.', flush=True)
            if not args.no_open:
                webbrowser.open(url)
            if 'url' not in cluster.config and cluster.config['hostname'] != socket.gethostname():
                print('SSH connection is open. Keep this command running while using the dashboard. Ctrl+C closes it.', flush=True)
                try:
                    while True:
                        time.sleep(1)
                except KeyboardInterrupt:
                    pass
        return 0
    if args.operation == 'cluster':
        action = args.cluster_operation
        if action == 'start':
            listen = start_options(args)
            cluster = Cluster.start(args.name, directory=args.directory, local_worker=not args.no_worker,
                                    slots=args.slots, memory=args.memory, cpus=args.cpus, gpus=args.gpus,
                                    listen=listen, tls_cert=args.tls_cert, tls_key=args.tls_key, token_file=args.token_file,
                                    monitor_interval=args.monitor_interval, history_hours=args.history_hours)
            if args.json and args.advertise:
                from ..sandbox.workspace import atomic_json
                atomic_json(Path(cluster.config['directory']) / 'connection-display.json',
                            {'advertise': validate_advertise(args.advertise)})
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
            if action == 'instructions' or (action == 'start' and not args.json):
                instructions(cluster, advertise=args.advertise)
            elif action in ('start', 'status'):
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
                    # Reject an unreachable controller or bad credential before
                    # installing runtimes or launching a persistent worker.
                    cluster.connection.control.timeout = 10
                    try:
                        cluster.connection.call('ping')
                    except Exception as error:
                        raise ValueError('Could not connect to the controller. Check that the printed host is reachable '
                                         'and copy a current join command from cluster instructions. ' + str(error)) from error
                    finally:
                        cluster.connection.control.timeout = 300
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
