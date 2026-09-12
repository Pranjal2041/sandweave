"""Launch the standalone Linux game in a GPU sandbox on the current worker."""
import argparse
import configparser
import io
import json
from pathlib import Path
import time

from sandweave import Memory, Mount, Recording, Sandbox


def configure(env):
    """Keep game preferences in the writable guest, outside the game download."""
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config.read_string(env.files.read_text('/opt/stuntrally3/config/game-default.cfg'))
    # The upstream INI format includes spaces in section names.
    sections = {name.strip(): name for name in config.sections()}
    for section, key, value in (
        ('misc', 'ogredialog', 'off'), ('misc', 'autostart', 'on'),
        ('misc', 'version', '3303'),
        ('game', 'start_in_main', 'off'), ('game', 'track', 'Isl12-Beach'),
        ('tweak', 'fps', '1'),
    ):
        config[sections[section]][key] = value
    output = io.StringIO()
    config.write(output)
    env.run('mkdir -p /home/ga/.config/stuntrally3', check=True)
    env.files.write_text('/home/ga/.config/stuntrally3/game.cfg', output.getvalue())
    env.files.write_text('/home/ga/.config/stuntrally3/ogre.cfg', '''Render System=OpenGL 3+ Rendering Subsystem

[OpenGL 3+ Rendering Subsystem]
Colour Depth=32
Display Frequency=N/A
FSAA=0
Full Screen=Yes
VSync=No
Video Mode=1920 x 1080
sRGB Gamma Conversion=Yes
''')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", type=Path, required=True)
    parser.add_argument("--virtualgl", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hold-seconds", type=int, default=3600)
    parser.add_argument('--record', action='store_true', help='Save a 30 FPS desktop recording')
    args = parser.parse_args()
    game_dir = args.game_dir.resolve()
    if not (game_dir / "bin/Release/stuntrally3").is_file():
        parser.error("--game-dir must contain the extracted Linux release")
    if not args.virtualgl.is_file() or args.hold_seconds < 1:
        parser.error('provide the VirtualGL .deb and a positive --hold-seconds')
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    with Sandbox(
        template='gnome', gpu=True, cpu=4,
        memory=Memory("6GiB", "1GiB"), ttl=args.hold_seconds + 600,
        mounts=[Mount(str(game_dir), "/opt/stuntrally3")],
        recording=Recording(fps=30, cursor=False) if args.record else False,
        startup_timeout=300,
    ) as env:
        try:
            play(env, args)
        finally:
            if args.record:
                env.recording.download(args.output / 'recording')


def play(env, args):
    (args.output / "sandbox.json").write_text(json.dumps(env.info, indent=2))
    (args.output / "sandbox.json").chmod(0o600)
    print(json.dumps(env.info, indent=2), flush=True)
    env.files.upload(args.virtualgl, "/tmp/virtualgl.deb")
    result = env.run(
        "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "/tmp/virtualgl.deb libxcb-keysyms1 libxss1 mesa-utils", user="root", timeout=300,
    )
    (args.output / "install.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError("VirtualGL installation failed; see install.log")
    result = env.run(
        "/usr/local/bin/engine-gpu /opt/VirtualGL/bin/vglrun -d egl0 glxinfo -B",
        timeout=30,
    )
    (args.output / "renderer.txt").write_text(result.stdout + result.stderr)
    print(result.stdout + result.stderr, flush=True)
    if result.returncode or "NVIDIA" not in result.stdout:
        raise RuntimeError("NVIDIA OpenGL rendering was not established")
    configure(env)
    process = env.exec(
        "exec /usr/local/bin/engine-gpu /opt/VirtualGL/bin/vglrun -d egl0 "
        "/opt/stuntrally3/bin/Release/stuntrally3",
        cwd="/opt/stuntrally3", env={"LD_LIBRARY_PATH": "/opt/stuntrally3/lib", "VGL_FPS": "60"},
    )
    (args.output / "process.json").write_text(json.dumps({"id": process.id}))
    print("Game launched", process.id, flush=True)
    time.sleep(30)
    if process.poll() is not None:
        result = process.result()
        (args.output / 'game.log').write_text(result.stdout + result.stderr)
        raise RuntimeError('Game exited during startup; see game.log')
    env.desktop.screenshot().save(args.output / "initial.png")
    print("Use arrow keys to drive, Backspace to reset, Escape for menus. Ctrl+C ends the sandbox.", flush=True)
    deadline = time.monotonic() + args.hold_seconds
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        env.files.download('/home/ga/.config/stuntrally3/Ogre.log', args.output / 'Ogre.log')


if __name__ == "__main__":
    main()
