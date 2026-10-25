"""Modal sandbox entrypoint for Sandweave templates, run as guest root without systemd.

Modal's gVisor cannot run systemd services (see notes/modal-runtime.md), so this
process replaces the template init for the parts the VR controls use: the NVIDIA
driver paths expected by engine-gpu, the guest user's runtime directory and session
bus, an X server on DISPLAY :1, and a PulseAudio server whose only (default) output
is the null sink guest_audio.py captures. It records readiness in /run/sandweave/boot.json
and exits when the X server exits. The guest agent is started separately.
Standard library only: this file runs from the template image's Python.
"""
import json
import os
from pathlib import Path
import pwd
import secrets
import subprocess
import sys
import time

STATE = Path('/run/sandweave')
DRIVER = Path('/opt/engine-gpu/driver')


def record(**value):
    STATE.mkdir(mode=0o755, exist_ok=True)
    temporary = STATE / 'boot.json.tmp'
    temporary.write_text(json.dumps({**value, 'monotonic_ns': time.monotonic_ns()}) + '\n')
    temporary.replace(STATE / 'boot.json')


def link_driver():
    """Expose the driver Modal injects into the image at the paths engine-gpu uses."""
    library = DRIVER / 'lib'
    library.mkdir(parents=True, exist_ok=True)
    listing = subprocess.run(['ldconfig', '-p'], capture_output=True, text=True, check=True).stdout
    linked = []
    for line in listing.splitlines():
        name, separator, path = line.partition('=>')
        name, path = name.split()[0] if name.split() else '', path.strip()
        if separator and ('nvidia' in name or name.startswith('libcuda')) and 'x86-64' in line:
            target = library / name
            if not target.exists():
                target.symlink_to(path)
                linked.append(name)
    for name, source in (('egl.json', '/usr/share/glvnd/egl_vendor.d/10_nvidia.json'),
                         ('vulkan.json', '/etc/vulkan/icd.d/nvidia_icd.json')):
        if Path(source).is_file():
            (DRIVER / name).unlink(missing_ok=True)
            (DRIVER / name).symlink_to(source)
    if 'libGLX_nvidia.so.0' not in {path.name for path in library.iterdir()}:
        raise RuntimeError('the Modal GPU driver does not provide libGLX_nvidia.so.0')
    return linked


def main():
    user = pwd.getpwnam(os.environ.get('SANDWEAVE_GUEST_USER', 'ga'))
    display = os.environ.get('SANDWEAVE_DISPLAY', ':1')
    geometry = os.environ.get('SANDWEAVE_GEOMETRY', '1920x1080')
    account = {'user': user.pw_uid, 'group': user.pw_gid, 'extra_groups': []}
    runtime = Path('/run/user') / str(user.pw_uid)
    environment = {'HOME': user.pw_dir, 'USER': user.pw_name, 'LOGNAME': user.pw_name,
                   'PATH': '/usr/local/bin:/usr/bin:/bin', 'XDG_RUNTIME_DIR': str(runtime)}
    try:
        gpu = Path('/dev/nvidiactl').exists()
        linked = link_driver() if gpu else []
        runtime.mkdir(parents=True, exist_ok=True)
        os.chown(runtime, user.pw_uid, user.pw_gid)
        runtime.chmod(0o700)
        sockets = Path('/tmp/.X11-unix')
        sockets.mkdir(exist_ok=True)
        sockets.chmod(0o1777)
        authority = Path(user.pw_dir) / '.Xauthority'
        authority.unlink(missing_ok=True)
        subprocess.run(['xauth', '-f', str(authority), 'add', display, '.', secrets.token_hex(16)],
                       env=environment, check=True, stdout=subprocess.DEVNULL, **account)
        logs = STATE / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        bus = subprocess.Popen(['dbus-daemon', '--session', '--nofork', '--nopidfile',
                                f'--address=unix:path={runtime}/bus'], env=environment,
                               stdin=subprocess.DEVNULL, stdout=(logs / 'session-bus.log').open('ab'),
                               stderr=subprocess.STDOUT, start_new_session=True, **account)
        server = subprocess.Popen(['Xtigervnc', display, '-geometry', geometry, '-depth', '24',
                                   '-SecurityTypes', 'None', '-localhost', '1', '-rfbport', '5901',
                                   '-auth', str(authority), '-AlwaysShared'],
                                  env=environment, stdin=subprocess.DEVNULL,
                                  stdout=(logs / 'xserver.log').open('ab'), stderr=subprocess.STDOUT,
                                  start_new_session=True, **account)
        deadline = time.monotonic() + 60
        while subprocess.run(['xdpyinfo', '-display', display], env={**environment, 'XAUTHORITY': str(authority)},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **account).returncode:
            if server.poll() is not None:
                raise RuntimeError('X server exited during startup: ' + (logs / 'xserver.log').read_text()[-2000:])
            if time.monotonic() > deadline:
                raise TimeoutError('X server did not accept connections')
            time.sleep(.1)
        if bus.poll() is not None:
            raise RuntimeError('session bus exited during startup')
        audio = subprocess.Popen(['pulseaudio', '--daemonize=no', '--exit-idle-time=-1', '--disallow-exit',
                                  '--disallow-module-loading', '-n', '--log-target=stderr',
                                  '--load=module-native-protocol-unix',
                                  '--load=module-null-sink sink_name=sandweave rate=48000 channels=2 '
                                  'sink_properties=device.description=Sandweave'],
                                 env=environment, stdin=subprocess.DEVNULL, stdout=(logs / 'pulseaudio.log').open('ab'),
                                 stderr=subprocess.STDOUT, start_new_session=True, **account)
        deadline = time.monotonic() + 30
        while (sink := subprocess.run(['pactl', 'get-default-sink'], env=environment, capture_output=True,
                                      text=True, **account)).stdout.strip() != 'sandweave':
            if audio.poll() is not None:
                raise RuntimeError('PulseAudio exited during startup: ' + (logs / 'pulseaudio.log').read_text()[-2000:])
            if time.monotonic() > deadline:
                raise TimeoutError('PulseAudio did not provide its sink: ' + sink.stderr[-500:])
            time.sleep(.1)
        record(ready=True, display=display, gpu=gpu, driver_links=linked, audio_sink='sandweave',
               region=os.environ.get('MODAL_REGION'), cloud=os.environ.get('MODAL_CLOUD_PROVIDER'))
    except Exception as error:
        record(ready=False, error=f'{type(error).__name__}: {error}')
        raise
    status = server.wait()
    record(ready=False, error=f'X server exited with status {status}')
    sys.exit(1)


if __name__ == '__main__':
    main()
