"""OSWorld tasks, original desktop recipe, and the canonical evaluator."""
import base64
import hashlib
from importlib import resources
from pathlib import Path
import shlex
import tempfile
import tomllib

from .benchmark import Evaluation
from . import source, evaluation
from ..sandbox.workspace import home
from ..templates.resolve import resolve


def template(reference):
    """Apply the original modal-native recipe inside the no-KVM guest."""
    base = source.module(reference / 'src/cua_speedrun/remote/osworld_modal_base.py')
    recipe = resolve(tomllib.loads(resources.files('sandweave.templates').joinpath(
        'osworld/base.toml').read_text()))
    delta = base.rendered_delta().encode()
    digest = hashlib.sha256(delta).hexdigest()
    encoded = base64.b64encode(delta).decode()
    # Mount a second view of / so the upstream chroot recipe can mount/unmount
    # its own proc, dev, sys and run without removing the guest's root mounts.
    script = f'''set -euo pipefail
for i in $(seq 0 63); do
    test -c /dev/tty$i || mknod -m 600 /dev/tty$i c 4 $i
done
if ! test -f /var/lib/sandweave-osworld-{digest}; then
    mkdir -p /.sandweave-build-root
    mount --bind / /.sandweave-build-root
    printf %s {shlex.quote(encoded)} | base64 -d > /tmp/osworld-delta.sh
    R=/.sandweave-build-root bash /tmp/osworld-delta.sh > /var/log/osworld-build.log 2>&1
    umount /.sandweave-build-root
    rmdir /.sandweave-build-root
    rm /tmp/osworld-delta.sh
    touch /var/lib/sandweave-osworld-{digest}
fi
exec env -i container=gvisor TERM=linux PATH=/usr/sbin:/usr/bin:/sbin:/bin /sbin/init "$@"
'''
    recipe['runtime_options']['init_command'] = ['/bin/bash', '-c', script, 'osworld-init']
    recipe['capabilities']['desktop']['keyboard_source'] = (
        reference / 'src/cua_speedrun/envs/_pyautogui_keyboard.py').read_text()
    recipe['name'] = 'osworld@' + source.CUA_REVISION
    return recipe, base


class OSWorld:
    def __init__(self, name, *, source=None):
        from .source import cua, tasks
        self.reference = cua(source)
        self.tasks = tasks(self.reference, name)
        self.evaluators = None

    def prepare(self):
        recipe, base = template(self.reference)
        verifier = self.reference / 'scripts/osworld_shared/osworld_verifier.py'
        python = evaluation.prepare(base, verifier)
        self.evaluators = evaluation.Evaluators(python, verifier)
        return {'template': recipe, 'startup_timeout': 3600}

    def setup(self, env, task):
        directory = Path(task.metadata['directory'])
        guest = '/workspace/tasks/' + directory.name
        env.run('mkdir -p /workspace/tasks/_shared', user='root', check=True)
        env.files.upload(directory, guest)
        env.files.upload(self.reference / 'scripts/osworld_shared/osworld_setup.py',
                         '/workspace/tasks/_shared/osworld_setup.py')
        try:
            env.run(argv=['python3', '/workspace/tasks/_shared/osworld_setup.py', guest + '/source.json'],
                    user='root', env={'OSWORLD_DESKTOP_USER': 'user', 'OSWORLD_DESKTOP_HOME': '/home/user',
                                      'OSWORLD_X11_DISPLAY': ':0'}, timeout=600, check=True)
        finally:
            env.run(argv=['rm', '-rf', '/workspace/tasks'], user='root', check=True)

    def evaluate(self, env, task):
        from .transport import GuestPort
        trajectory = env._call('control', name='desktop', method='history', parameters={})
        root = home() / 'benchmarks/evaluations'
        root.mkdir(parents=True, exist_ok=True)
        with GuestPort(env, 22) as port, tempfile.TemporaryDirectory(prefix=task.id + '-', dir=root) as directory:
            result = self.evaluators.run({'source': str(Path(task.metadata['directory']) / 'source.json'),
                'trajectory': trajectory, 'env_info': {'ssh_port': port, 'ssh_user': 'user',
                 'ssh_password': 'password', 'episode_dir': directory}})
        return Evaluation(task.id, result['score'], result['passed'], result.get('feedback', ''))

    def close(self):
        if self.evaluators is not None:
            self.evaluators.close()
