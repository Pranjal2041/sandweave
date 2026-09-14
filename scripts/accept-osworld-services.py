#!/usr/bin/env python3
"""Boot the OSWorld template and exercise its optional Linux service support."""
import argparse
import json
from pathlib import Path
import time

from sandweave import Sandbox
from sandweave.benchmarks import source
from sandweave.benchmarks.osworld import template


def check(env, output):
    output.mkdir(parents=True, exist_ok=True)
    (output / 'sandbox-id').write_text(env.id)
    env.desktop.screenshot().save(output / 'desktop.png')
    env.files.upload(Path(__file__).with_name('probe-osworld-services.py'), '/tmp/probe-services.py')
    result = env.run('python3 /tmp/probe-services.py', user='root', timeout=45)
    (output / 'probe.json').write_text(result.stdout)
    (output / 'probe.stderr').write_text(result.stderr)
    journal = env.run('journalctl -b --no-pager -u avahi-daemon -u setvtrgb -u systemd-sysctl', user='root')
    (output / 'journal.txt').write_text(journal.stdout)
    print(json.dumps({'sandbox': env.id, 'returncode': result.returncode,
                      'result': json.loads(result.stdout)}), flush=True)
    assert result.returncode == 0, result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache', help='existing prepared OSWorld filesystem cache')
    parser.add_argument('--save-cache', help='retain the prepared filesystem for subsequent acceptance')
    parser.add_argument('--restore-live', action='store_true', help='repeat checks after a live snapshot restore')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    recipe, _ = template(source.cua(args.source))
    started = time.monotonic()
    options = {'cache': args.cache} if args.cache else {'template': recipe}
    with Sandbox(**options, startup_timeout=3600) as env:
        check(env, args.output)
        if args.save_cache:
            saved = env.cache(args.save_cache)
            (args.output / 'cache').write_text(str(saved))
        if args.restore_live:
            live = env.snapshot(state='memory')
    if args.restore_live:
        with Sandbox(snapshot=live.id) as restored:
            check(restored, args.output / 'restored')
    (args.output / 'timing.json').write_text(json.dumps({'seconds': time.monotonic() - started}))


if __name__ == '__main__':
    main()
