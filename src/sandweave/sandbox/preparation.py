"""Install a local template before choosing its immutable worker workspace."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import importlib
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid

from . import workspace
from .errors import ResourceUnavailable, SetupError

_checking = ContextVar('sandweave_installation_check', default=None)


@dataclass(frozen=True)
class Installation:
    directory: Path
    assets: Path


@contextmanager
def checking(directory, assets):
    token = _checking.set(Installation(Path(directory), Path(assets)))
    try:
        yield
    finally:
        _checking.reset(token)


def directory():
    from ..onboarding import configuration
    selected = workspace.home()
    if os.environ.get('SANDWEAVE_HOME') or configuration(selected).get('assets'):
        return selected
    return (Path.cwd() / '.sandweave').resolve()


def source(directory):
    """An explicit source can have a managed installation extended by setup."""
    from ..onboarding import configuration
    config = configuration(directory)
    override = os.environ.get('SANDWEAVE_ASSETS')
    identity = workspace.asset_identity(override) if override else None
    installed = config.get('installed_sources', {})
    if not isinstance(installed, dict):
        raise ResourceUnavailable('Installed runtime sources must be an object in config.json')
    selected = installed.get(identity) or override or config.get('assets')
    try:
        return workspace.assets(directory=directory, selected=selected), identity
    except ResourceUnavailable:
        if override and selected != override:
            # A removed managed copy can be recreated from its original source.
            return workspace.assets(directory=directory, selected=override), identity
        raise


def available(recipe, directory):
    """Check required files without hashing images or starting host probes."""
    from ..onboarding import configuration, python_packages, validate_assets
    from ..installation import needs_helpers
    directory = Path(directory)
    # First use still needs to establish storage when a legacy source happens
    # to be discoverable from the working directory.
    if not configuration(directory).get('assets'):
        return None
    try:
        root, _ = source(directory)
        validate_assets(root, recipe, contents=False)
        if needs_helpers(root, recipe):
            return None
        if not workspace.tool('apptainer'):
            return None
        if 'vr' in recipe['capabilities'] and not workspace.tool('ffmpeg'):
            return None
        if any(importlib.util.find_spec(module) is None for module, _ in python_packages(recipe)):
            return None
    except (OSError, ValueError, KeyError, TypeError, ResourceUnavailable):
        return None
    return Installation(directory, root)


def ensure(recipe):
    candidate = _checking.get()
    if candidate is not None:
        return candidate
    from ..onboarding import workload
    selected = directory()
    candidate = available(recipe, selected)
    if candidate is not None:
        return candidate
    from ..installation import destination
    selected = destination(selected)
    profile = workload(recipe)
    print('Preparing ' + profile + ' in ' + str(selected) + ' (first use)...', file=sys.stderr, flush=True)
    _install(profile, selected)
    importlib.invalidate_caches()
    candidate = available(recipe, selected)
    if candidate is None:
        raise SetupError('Installation did not provide all files required by ' + recipe['name'] +
                         '. Run sandweave doctor --template ' + profile + ' to see the failed checks.',
                         phase='installation')
    return candidate


def _install(profile, selected):
    # Setup changes its environment while building/checking a candidate. Run
    # it in its own process so concurrent clients and workers keep their paths.
    logs = selected / 'logs/setup'
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / ('first-use-' + uuid.uuid4().hex + '.log')
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[2]) +
                   os.pathsep + os.environ.get('PYTHONPATH', ''), 'PYTHONUNBUFFERED': '1'}
    command = [sys.executable, '-m', 'sandweave.sandbox.preparation',
               '--template', profile, '--directory', str(selected)]
    with log.open('w') as output:
        log.chmod(0o600)
        process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors='replace', start_new_session=True)
        try:
            for line in process.stdout:
                output.write(line)
                output.flush()
                print(line, end='', file=sys.stderr, flush=True)
            code = process.wait()
        except BaseException:
            if process.poll() is None:
                # SIGINT lets the installer's build runner release its own
                # subprocesses and retain the interrupted build for retry.
                os.killpg(process.pid, signal.SIGINT)
                process.wait()
            raise
        finally:
            process.stdout.close()
    if code:
        raise SetupError('Could not prepare ' + profile + '. Installation log: ' + str(log), phase='installation')


def main():
    import argparse
    from .. import onboarding
    parser = argparse.ArgumentParser()
    parser.add_argument('--template', required=True, choices=onboarding.PROFILES)
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    args.yes, args.assets, args.game_archive, args._automatic = True, None, None, True
    try:
        override = os.environ.get('SANDWEAVE_ASSETS')
        args._source_identity = workspace.asset_identity(override) if override else None
        return onboarding.setup_worker(args, args.template, interactive=False)
    except KeyboardInterrupt:
        print('Installation interrupted. Creating the sandbox again will retry.', file=sys.stderr)
        return 130
    except Exception as error:
        print('Installation failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
