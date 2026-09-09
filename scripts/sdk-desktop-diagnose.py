#!/usr/bin/env python3
"""Collect disposable SDK desktop readiness evidence; never attach to an existing desktop."""
import json
import argparse
from pathlib import Path
import time

from sandweave import Sandbox, Memory, Slurm

parser = argparse.ArgumentParser()
parser.add_argument('--gpu')
parser.add_argument('--base-snapshot')
parser.add_argument('--keep', action='store_true')
parser.add_argument('--job')
args = parser.parse_args()

root = Path('runs/sdk-acceptance/desktop-diagnose')
root.mkdir(parents=True, exist_ok=True)
recipe = {'extends': 'gnome@1'}
if args.base_snapshot:
    recipe['base_snapshot'] = args.base_snapshot
target = Slurm.connect(args.job, cpus=12) if args.job else None
try:
    env = Sandbox(template=recipe, gpu=args.gpu, cpu=8, memory=Memory('12GiB', '2GiB'),
                  name='sdk-desktop-diagnostic', keep_on_error=args.keep, target=target)
except Exception as error:
    if not args.keep or not getattr(error, 'sandbox_id', None):
        raise
    print('Retained for diagnosis:', error.sandbox_id, str(error), flush=True)
    env = Sandbox.connect(error.sandbox_id, target=target)
try:
    report = {'id': env.id, 'timings': env.timings, 'samples': []}
    if env.status()['state'] == 'ready':
        env.desktop.keyboard.press(['ESC'])
        time.sleep(.5)
    source = '''import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
from pathlib import Path
w=Gtk.Window(title="Sandweave diagnostic")
w.set_default_size(760, 220)
e=Gtk.Entry()
e.connect("changed", lambda e: Path("/workspace/typed").write_text(e.get_text()))
w.add(e); w.show_all(); e.grab_focus(); Gtk.main()
'''
    app = env.exec(argv=['python', '-u', '-c', source])
    for i in range(4):
        time.sleep(15)
        result = env.run("ps -eo pid,ppid,stat,comm; xprop -root _NET_SUPPORTING_WM_CHECK _NET_ACTIVE_WINDOW; "
                         "xdotool getwindowfocus getwindowname; xwininfo -root -tree", check=False)
        sample = {'index': i, 'process_status': app.poll(), 'stdout': result.stdout, 'stderr': result.stderr}
        if app.poll() is not None:
            sample['application'] = vars(app.result())
        try:
            env.desktop.screenshot().save(root / f'{i}.png')
        except Exception as error:
            sample['screenshot_error'] = str(error)
        focus = env.run("xdotool search --sync --name 'Sandweave diagnostic' windowactivate --sync", timeout=5, check=False)
        sample['activate'] = vars(focus)
        try:
            sample['action'] = env.desktop.keyboard.type('hello € 日本語')
        except Exception as error:
            sample['action_error'] = str(error)
        report['samples'].append(sample)
        sample['journal'] = env.run('journalctl --no-pager _UID=1000 -n 60; '
            'runuser -u ga -- env XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus '
            'systemctl --user list-jobs --no-pager; tail -n 45 /home/ga/.vnc/*.log 2>&1', user='root', check=False).stdout
        (root / 'report.json').write_text(json.dumps(report, indent=2))
finally:
    if not args.keep:
        env.terminate()
    env.close()
