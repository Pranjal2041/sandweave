#!/usr/bin/env python3
"""Collect disposable SDK desktop readiness evidence; never attach to an existing desktop."""
import json
from pathlib import Path
import time

from sandweave import Sandbox

root = Path('runs/sdk-acceptance/desktop-diagnose')
root.mkdir(parents=True, exist_ok=True)
with Sandbox(template='gnome') as env:
    report = {'id': env.id, 'timings': env.timings, 'samples': []}
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
        result = env.run("ps -ef; xprop -root _NET_SUPPORTING_WM_CHECK _NET_ACTIVE_WINDOW; "
                         "xdotool getwindowfocus getwindowname; xwininfo -root -tree", check=False)
        sample = {'index': i, 'process_status': app.poll(), 'stdout': result.stdout, 'stderr': result.stderr}
        if app.poll() is not None:
            sample['application'] = vars(app.result())
        env.desktop.screenshot().save(root / f'{i}.png')
        focus = env.run("xdotool search --sync --name 'Sandweave diagnostic' windowactivate --sync", timeout=5, check=False)
        sample['activate'] = vars(focus)
        try:
            sample['action'] = env.desktop.keyboard.type('hello € 日本語')
        except Exception as error:
            sample['action_error'] = str(error)
        report['samples'].append(sample)
        sample['journal'] = env.run('journalctl --no-pager -n 60', user='root').stdout
        (root / 'report.json').write_text(json.dumps(report, indent=2))
