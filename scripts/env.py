#!/usr/bin/env python3
"""Manage standalone no-KVM environments; prints JSON results."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

from environment import EnvironmentManager


def launch_arguments(arguments, default_command):
    if '--' in arguments:
        split = arguments.index('--')
        command = arguments[split + 1:]
        if not command:
            raise ValueError('provide a guest command after --')
        return arguments[:split], command
    return arguments, default_command


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    launch_tail = []
    if '--launch-options' in argv:
        split = argv.index('--launch-options')
        launch_tail, argv = argv[split + 1:], argv[:split + 1]
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    listing = commands.add_parser('list')
    listing.add_argument('--active', action='store_true')
    for action in ('status', 'pause', 'resume'):
        commands.add_parser(action).add_argument('name')
    for action in ('save', 'stop'):
        cmd = commands.add_parser(action)
        cmd.add_argument('name')
        if action == 'save':
            cmd.add_argument('label')
            cmd.add_argument('--local-only', action='store_true')
        else:
            cmd.add_argument('--label')
            cmd.add_argument('--discard', action='store_true', help='terminate without saving; unsaved RAM and writable files are lost')
        mode = cmd.add_mutually_exclusive_group()
        mode.add_argument('--filesystem', action='store_const', const='filesystem', dest='mode')
        mode.add_argument('--experimental-gpu-live', action='store_const', const='experimental-gpu-live', dest='mode')
        cmd.set_defaults(mode='auto')
    start = commands.add_parser('start')
    start.add_argument('name')
    start.add_argument('--launch-options', action='store_true', help='run-gvisor.py options, optionally followed by -- and a guest command')
    for action in ('load', 'restore'):
        cmd = commands.add_parser(action)
        cmd.add_argument('snapshot', type=Path)
        cmd.add_argument('name')
        cmd.add_argument('--experimental-gpu-live', action='store_true')
        cmd.add_argument('--verify', action='store_true')
        cmd.add_argument('--launch-options', action='store_true')
    args = parser.parse_args(argv)
    manager = EnvironmentManager()
    try:
        if args.action == 'list':
            result = manager.list(active_only=args.active)
        elif args.action in ('status', 'pause', 'resume'):
            result = getattr(manager, args.action)(args.name)
        elif args.action == 'save':
            result = manager.save(args.name, args.label, mode=args.mode, local_only=args.local_only)
        elif args.action == 'stop':
            result = manager.stop(args.name, discard=args.discard, label=args.label, mode=args.mode)
        elif args.action == 'start':
            options, command = launch_arguments(launch_tail, ['/sbin/init'])
            result = manager.start(args.name, options=options, command=command)
        else:
            options, command = launch_arguments(launch_tail, [])
            result = manager.load(args.snapshot, args.name, experimental_gpu_live=args.experimental_gpu_live,
                                  verify=args.verify, options=options, command=command)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
