#!/usr/bin/env python3
"""Prepare a template using a completed, managed CPU build as its base."""
import argparse
import json
from pathlib import Path
import time

from sandweave.bootstrap import Builder
from sandweave.installation import using_directory
from sandweave.sandbox import workspace
from sandweave.templates.resolve import Template


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-result', required=True, type=Path)
    parser.add_argument('--template', required=True)
    parser.add_argument('--result', required=True, type=Path)
    parser.add_argument('--wait-seconds', type=int, default=600)
    args = parser.parse_args()
    if args.result.exists():
        parser.error('result already exists')
    deadline = time.monotonic() + args.wait_seconds
    while not args.build_result.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError('CPU runtime build did not finish before the deadline')
        time.sleep(2)
    base = Path(json.loads(args.build_result.read_text())['assets'])
    directory = base.parent.parent
    recipe = Template(args.template).resolve()
    with using_directory(directory):
        assets = Builder(directory).build(args.template, recipe, base=base)
        workspace.atomic_json(args.result, {'assets': str(assets), 'template': args.template,
                                           'base_build_result': str(args.build_result)})
    print(args.result.read_text(), flush=True)


if __name__ == '__main__':
    main()
