"""CLI commands call the same public SDK and preserve guest stdout/stderr/exit status."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import threading
import time

from . import Sandbox, SandboxError, CommandTimeout
from .sandbox.snapshots import SnapshotRef
from .sandbox.targets import connect
from .sandbox.workspace import home, atomic_json


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
    parser.add_argument('--setup')
    parser.add_argument('--cache')
    parser.add_argument('--snapshot')
    parser.add_argument('--cache-key')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--runtime', default='gvisor', choices=('gvisor', 'apptainer'))
    parser.add_argument('--target')
    parser.add_argument('--cpu', type=int)
    parser.add_argument('--memory')
    parser.add_argument('--gpu', help='auto, none, or an allocated model name')
    parser.add_argument('--network', choices=('internet', 'offline'))
    parser.add_argument('--name')
    parser.add_argument('--ttl', type=float)


def command_options(parser):
    parser.add_argument('--argv', action='store_true', help='execute literal arguments without a guest shell')
    parser.add_argument('--timeout', type=float)
    parser.add_argument('command', nargs=argparse.REMAINDER)


def creation(args):
    keys = ('template', 'setup', 'cache', 'snapshot', 'cache_key', 'refresh', 'runtime',
            'target', 'cpu', 'memory', 'gpu', 'network', 'name', 'ttl')
    options = {k: getattr(args, k) for k in keys if getattr(args, k, None) is not None}
    if options.get('gpu') in ('auto', 'none'):
        options['gpu'] = options['gpu'] == 'auto'
    return options


def execute(env, args):
    arguments = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not arguments or (not args.argv and len(arguments) != 1):
        raise ValueError('provide one quoted command string after --, or use --argv for literal arguments')
    process = env.exec(argv=arguments, timeout=args.timeout, binary=True) if args.argv else env.exec(
        arguments[0], timeout=args.timeout, binary=True)
    process.stdin.close()
    errors = []

    def drain(source, destination):
        try:
            # read(size) waits to fill size; chunk/readline would delay binary
            # output. Read available chunks and check completion only at EOF.
            while True:
                data = source._chunk()
                if data:
                    destination.write(data); destination.flush()
                elif process.poll() is not None:
                    break
                else:
                    time.sleep(.01)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=drain, args=(source, destination)) for source, destination in
               ((process.stdout, sys.stdout.buffer), (process.stderr, sys.stderr.buffer))]
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
        for thread in threads:
            thread.join()
    if errors:
        raise errors[0]
    return code if code >= 0 else 128 - code


def parser():
    root = argparse.ArgumentParser(prog='sandweave', description='Sandboxes without host sudo or KVM')
    sub = root.add_subparsers(dest='operation', required=True)
    create = sub.add_parser('create'); creation_options(create)
    run = sub.add_parser('run'); creation_options(run); command_options(run)
    existing = sub.add_parser('exec'); existing.add_argument('--target'); existing.add_argument('id'); command_options(existing)
    listing = sub.add_parser('list'); listing.add_argument('--target'); listing.add_argument('--all', action='store_true')
    for name in ('status', 'pause', 'resume', 'terminate', 'stop', 'snapshot'):
        p = sub.add_parser(name); p.add_argument('id'); p.add_argument('--target')
        if name in ('stop', 'snapshot'):
            p.add_argument('--state', default='auto' if name == 'stop' else 'memory',
                           choices=('auto', 'memory', 'filesystem'))
    setup = sub.add_parser('setup'); setup.add_argument('id'); setup.add_argument('script'); setup.add_argument('--target')
    cache = sub.add_parser('cache').add_subparsers(dest='cache_operation', required=True)
    save = cache.add_parser('save'); save.add_argument('id'); save.add_argument('key'); save.add_argument('--target')
    save.add_argument('--state', default='filesystem', choices=('filesystem', 'memory'))
    for operation in ('inspect', 'verify'):
        p = cache.add_parser(operation); p.add_argument('reference'); p.add_argument('--target')
    files = sub.add_parser('files').add_subparsers(dest='file_operation', required=True)
    for operation in ('upload', 'download'):
        p = files.add_parser(operation); p.add_argument('id'); p.add_argument('source'); p.add_argument('destination'); p.add_argument('--target')
    desktop = sub.add_parser('desktop').add_subparsers(dest='desktop_operation', required=True)
    shot = desktop.add_parser('screenshot'); shot.add_argument('id'); shot.add_argument('--output', required=True); shot.add_argument('--target')
    step = desktop.add_parser('step'); step.add_argument('id'); step.add_argument('action', help='JSON action'); step.add_argument('--output', required=True); step.add_argument('--target')
    record = sub.add_parser('vr').add_subparsers(dest='vr_operation', required=True).add_parser('record')
    record.add_argument('id'); record.add_argument('--duration', type=float, required=True); record.add_argument('--output', required=True)
    record.add_argument('--fps', type=int, default=30); record.add_argument('--target')
    configure = sub.add_parser('configure'); configure.add_argument('--assets', required=True)
    return root


def main(argv=None):
    arguments = parser()
    args = arguments.parse_args(argv)
    try:
        op = args.operation
        if op == 'configure':
            path = Path(args.assets).expanduser().resolve()
            if not path.is_dir():
                raise FileNotFoundError(path)
            atomic_json(home() / 'config.json', {'assets': str(path)})
            return 0
        if op == 'create':
            env = Sandbox(**creation(args))
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
            if op == 'exec':
                return execute(env, args)
            if op in ('status', 'pause', 'resume', 'terminate'):
                output(getattr(env, op)())
            elif op in ('stop', 'snapshot'):
                output(getattr(env, op)(state=args.state))
            elif op == 'setup':
                env.setup(args.script)
            elif op == 'cache':
                output(env.cache(args.key, state=args.state))
            elif op == 'files':
                getattr(env.files, args.file_operation)(args.source, args.destination)
            elif op == 'desktop':
                image = env.desktop.screenshot() if args.desktop_operation == 'screenshot' else env.desktop.step(json.loads(args.action)).image
                image.save(args.output)
            elif op == 'vr':
                if args.duration <= 0:
                    raise ValueError('recording duration must be positive')
                with env.vr.record(args.output, fps=args.fps):
                    time.sleep(args.duration)
        return 0
    except (SandboxError, ValueError, OSError, TimeoutError) as error:
        print(f'sandweave: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
