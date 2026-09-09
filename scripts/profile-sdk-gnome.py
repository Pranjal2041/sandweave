#!/usr/bin/env python3
"""Measure a disposable GNOME desktop and retain its first frame and boot logs."""
import argparse
import json
import os
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--cpu', type=int, default=4)
    parser.add_argument('--trace-bus', action='store_true')
    parser.add_argument('--reuse', action='store_true', help='also measure CPU desktop memory restore and a warm pool')
    parser.add_argument('--fresh-settings', action='store_true', help='clear first-run settings in the disposable guest')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    from sandweave import Sandbox

    started = time.monotonic()
    template = {'extends': 'gnome'}
    if args.fresh_settings:
        template['runtime_options'] = {'init_command': ['sh', '-c',
            'rm -f /home/ga/.config/dconf/user /home/ga/.local/share/gnome-shell/lock-warning-shown; '
            'exec /usr/local/bin/engine-gnome-init']}
    if args.trace_bus:
        template['services'] = {'system-bus-trace': {
            'command': 'dbus-monitor --system > /workspace/system-bus.log 2>&1', 'user': 'root'}}
    with Sandbox(template=template, cpu=args.cpu, ttl=600) as env:
        elapsed = time.monotonic() - started
        report = {'id': env.id, 'constructor_seconds': elapsed, 'timings': env.timings,
                  'cpu': args.cpu, 'gpu': False, 'host_cpus': len(os.sched_getaffinity(0))}
        (output / 'timings.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report), flush=True)
        env.desktop.screenshot().save(output / 'first-frame.png')
        commands = {
            'session-journal.txt': ('journalctl -b _UID=1000 -o short-monotonic --no-pager', 'root'),
            'user-units.txt': ('systemd-analyze --user blame; systemctl --user --failed --no-pager; '
                               'systemctl --user list-jobs --no-pager', 'ga'),
            'system-units.txt': ('systemd-analyze blame; systemctl --failed --no-pager; '
                                 'systemctl list-jobs --no-pager', 'root'),
            'desktop-startup.txt': ('for f in /home/ga/.vnc/xstartup /etc/X11/Xtigervnc-session '
                                    '/etc/X11/Xvnc-session /usr/lib/systemd/user/gnome-keyring.service; '
                                    'do if test -f "$f"; then printf "\\n%s\\n" "$f"; cat "$f"; fi; done; '
                                    'ps -u ga -o pid,comm; xwininfo -root -tree', 'root'),
        }
        for filename, (command, user) in commands.items():
            result = env.run(command, user=user, check=False, timeout=30)
            (output / filename).write_text(result.stdout + result.stderr)
        if args.trace_bus:
            env.files.download('/workspace/system-bus.log', output / 'system-bus.txt')
            result = env.run('journalctl -b -u dbus -u accounts-daemon -u systemd-logind '
                             '-o short-monotonic --no-pager', user='root', check=False)
            (output / 'system-journal.txt').write_text(result.stdout + result.stderr)
        print('Captured desktop and startup diagnostics in ' + str(output), flush=True)
        if args.reuse:
            shell_pid = env.run('pgrep -x gnome-shell').stdout.strip()
            started = time.monotonic()
            saved = env.snapshot(state='memory')
            reuse = {'capture_seconds': time.monotonic() - started}
            started = time.monotonic()
            assert saved.verify()['status'] == 'passed'
            reuse['verification_seconds'] = time.monotonic() - started
    if args.reuse:
        started = time.monotonic()
        with Sandbox(snapshot=saved, ttl=600) as restored:
            reuse['restore_constructor_seconds'] = time.monotonic() - started
            reuse['restore_timings'] = restored.timings
            assert restored.run('pgrep -x gnome-shell').stdout.strip() == shell_pid
            restored.desktop.screenshot().save(output / 'restored-first-frame.png')
            restored.desktop.keyboard.press('super')
            deadline = time.monotonic() + 5
            while restored.run('gdbus call --session --dest org.gnome.Shell '
                '--object-path /org/gnome/Shell --method org.freedesktop.DBus.Properties.Get '
                'org.gnome.Shell OverviewActive').stdout.strip() != '(<true>,)':
                if time.monotonic() >= deadline:
                    raise TimeoutError('restored desktop did not respond to keyboard input')
                time.sleep(.05)
            # Input acknowledgement and the visible property precede animation
            # completion. This delay is only for the diagnostic image.
            time.sleep(.5)
            restored.desktop.screenshot().save(output / 'restored-after-input.png')
        from sandweave import Pool
        started = time.monotonic()
        with Pool(snapshot=saved, size=1, warm=1) as pool:
            reuse['pool_prepare_seconds'] = time.monotonic() - started
            started = time.monotonic()
            with pool.acquire() as ready:
                reuse['pool_checkout_seconds'] = time.monotonic() - started
                frame = ready.desktop.screenshot()
                reuse['pool_checkout_and_frame_seconds'] = time.monotonic() - started
                frame.save(output / 'pool-first-frame.png')
        (output / 'reuse.json').write_text(json.dumps(reuse, indent=2) + '\n')
        print(json.dumps(reuse), flush=True)


if __name__ == '__main__':
    main()
