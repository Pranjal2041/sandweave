#!/usr/bin/env python3
"""Xvnc fast I/O CLI; uses the persistent per-environment service."""
import argparse
import json
from environment import EnvironmentManager
from fast_io import FastIOClient, detach, serve
import environment_control as control


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['screenshot', 'action', 'step', 'close', '_serve'])
    parser.add_argument('name')
    parser.add_argument('--backend', choices=['xvnc'], default='xvnc')
    parser.add_argument('--action', type=json.loads, help='Gym-style mouse/keyboard JSON; list batches gestures')
    parser.add_argument('--output', help='PNG/JPEG output path for screenshot or step')
    parser.add_argument('--latest', action='store_true', help='return the last captured frame, possibly stale')
    args = parser.parse_args()
    manager = EnvironmentManager()
    if args.operation == '_serve':
        serve(manager, args.name)
        return
    if args.operation == 'close':
        with control.acquire_lock(manager.local, args.name):
            print(json.dumps(detach(manager, args.name, stop=True)))
        return
    if args.operation in ('screenshot', 'step') and not args.output:
        parser.error('--output is required')
    if args.operation in ('action', 'step') and args.action is None:
        parser.error('--action is required')
    with FastIOClient(args.name, manager=manager, backend=args.backend) as client:
        if args.operation == 'action':
            result = client.action(args.action)
        else:
            image = client.screenshot(fresh=not args.latest) if args.operation == 'screenshot' else client.step(args.action)[0]
            image.save(args.output)
            result = {**client.last_metadata, 'output': args.output}
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
