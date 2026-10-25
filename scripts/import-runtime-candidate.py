#!/usr/bin/env python3
"""Import prepared workload images with an explicitly selected candidate engine."""
import argparse
import json
from pathlib import Path

from sandweave import releases
from sandweave.installation import destination, import_runtime, publish, using_directory
from sandweave.sandbox import workspace
from sandweave.templates.resolve import Template


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--template', required=True)
    parser.add_argument('--runtime-build', required=True)
    args = parser.parse_args()
    root = destination(args.directory)
    with using_directory(root):
        engine = releases.install(root, releases.check_host(root))
        if engine is None:
            raise RuntimeError('The pinned candidate engine is unavailable')
        descriptor = releases.validate_engine(engine)
        if descriptor['path'] != args.runtime_build:
            raise RuntimeError('Unexpected candidate engine: ' + descriptor['path'])
        assets = import_runtime(args.source, root, Template(args.template).resolve(), engine=engine)
        publish(root, assets, template=args.template,
                source_identity=workspace.asset_identity(args.source))
        print(json.dumps({'assets': str(assets), 'runtime': descriptor}), flush=True)


if __name__ == '__main__':
    main()
