#!/usr/bin/env python3
"""Operate the experimental VR stack in an already prepared disposable GPU sandbox."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from environment import EnvironmentManager


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', help='disposable sandbox name, starting with vr-')
    commands = parser.add_subparsers(dest='command', required=True)
    start = commands.add_parser('start')
    start.add_argument('--mirror', choices=['sync', 'pbo', 'none'], default='pbo')
    start.add_argument('--hz', type=int, choices=[60, 72, 90, 120, 144], default=90)
    commands.add_parser('stop')
    capture = commands.add_parser('capture')
    capture.add_argument('--output', type=Path)
    commands.add_parser('play')
    control = commands.add_parser('input')
    control.add_argument('input_args', nargs=argparse.REMAINDER)
    metrics = commands.add_parser('metrics')
    metrics.add_argument('--tail-seconds', type=float, default=30)
    args = parser.parse_args()
    if not args.name.startswith('vr-'):
        parser.error('use a disposable sandbox named vr-*')
    manager = EnvironmentManager()
    status = manager.status(args.name)
    if status['status'] != 'running' or not status.get('gpu'):
        parser.error('the selected sandbox must be running with GPU access')
    prefix = [*manager._command(args.name), 'exec', args.name]

    def guest(*command):
        return manager._run([*prefix, *command])

    output = manager.lab / 'runs/vr' / args.name
    output.mkdir(parents=True, exist_ok=True)
    if args.command == 'start':
        # Refuse to disrupt an existing VR session. Stop it explicitly first.
        if guest('sh', '-c', 'pgrep -x monado-service || true').strip():
            raise RuntimeError('Monado is already running; stop this experiment before starting another')
        guest('test', '-x', '/opt/vr/monado/bin/monado-service')
        guest('test', '-f', '/opt/vr/vr-monado-guest.sh')
        guest('test', '!', '-e', '/dev/kvm')
        # A crashed service can leave its UNIX socket behind; no live daemon above.
        guest('rm', '-f', '/run/user/1000/monado_comp_ipc')
        guest('systemd-run', '--unit=vr-monado-live', '--uid=ga', '--collect',
              '--setenv=VR_HZ=' + str(args.hz), '/usr/local/bin/engine-gpu',
              'sh', '/opt/vr/vr-monado-guest.sh', 'monado')
        # The socket exists before initialization finishes. Follow only this
        # service invocation's log while it creates its Vulkan resources.
        time.sleep(.2)
        invocation = guest('systemctl', 'show', 'vr-monado-live', '-p', 'InvocationID', '--value').strip()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            text = guest('journalctl', '_SYSTEMD_INVOCATION_ID=' + invocation, '-n', '200', '--no-pager')
            if 'Supported formats:' in text and guest('systemctl', 'is-active', 'vr-monado-live').strip() == 'active':
                break
            time.sleep(.2)
        else:
            raise RuntimeError('Monado did not initialize; inspect journalctl -u vr-monado-live')
        guest('systemd-run', '--unit=vr-open-saber-live', '--uid=ga', '--collect',
              '--setenv=VGL_READBACK=' + args.mirror, '/usr/local/bin/engine-gpu-gl',
              'sh', '/opt/vr/vr-monado-guest.sh', 'game')
        print(json.dumps({'name': args.name, 'compositor_target_hz': args.hz,
                          'mirror_readback': args.mirror, 'vnc_port': status['ports']['5901'],
                          'status': 'launched; verify the XR session and a captured eye image'}))
    elif args.command == 'stop':
        guest('systemctl', 'stop', 'vr-open-saber-live', 'vr-monado-live')
    elif args.command in ('play', 'input'):
        options = args.input_args if args.command == 'input' else [
            '--aim-at', '-0.109375', '0.953125', '-2', '--aim-pitch-offset', '-45', '--click', 'trigger']
        if options[:1] == ['--']:
            options = options[1:]
        print(guest('python3', '/opt/vr/vr-remote-input.py', *options).strip())
    elif args.command == 'capture':
        guest('runuser', '-u', 'ga', '--', 'touch', '/tmp/monado-eye.ppm.request')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if guest('sh', '-c', 'test -e /tmp/monado-eye.ppm.request && echo pending || true').strip() != 'pending':
                break
            time.sleep(.05)
        else:
            raise TimeoutError('Monado did not produce an eye image within 10 seconds')
        ppm = subprocess.run([*prefix, 'cat', '/tmp/monado-eye.ppm'], cwd=manager.lab,
                             capture_output=True, check=True, timeout=10).stdout
        import io
        from PIL import Image
        destination = args.output or output / ('eye-' + str(time.time_ns()) + '.png')
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.open(io.BytesIO(ppm)).save(destination)
        print(destination)
    elif args.command == 'metrics':
        path = output / ('metrics-' + str(time.time_ns()) + '.pb')
        with path.open('wb') as stream:
            subprocess.run([*prefix, 'cat', '/tmp/monado-metrics.pb'], cwd=manager.lab,
                           stdout=stream, check=True, timeout=10)
        result = subprocess.check_output([sys.executable, str(manager.lab / 'scripts/vr-metrics.py'),
                                          str(path), '--tail-seconds', str(args.tail_seconds)], text=True)
        path.with_suffix('.json').write_text(result)
        print(result, end='')


if __name__ == '__main__':
    main()
