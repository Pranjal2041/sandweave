"""Resolve host helpers, using installed Debian tools when the host lacks them."""
import os
from pathlib import Path
import resource
import shutil
import sys


def limited(command):
    return [sys.executable, str(Path(__file__).resolve()), 'limit', *command]


def command(lab, local, name, *arguments, memory_limit=False):
    executable = shutil.which(name)
    if executable:
        result = [executable, *arguments]
        return limited(result) if memory_limit else result
    helpers = Path(lab) / 'tools/helpers'
    candidates = [helpers / directory / name for directory in ('usr/bin', 'usr/sbin', 'bin', 'sbin')]
    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        raise RuntimeError(name + ' is missing; run sandweave setup to install runtime utilities')
    apptainer = shutil.which('apptainer')
    if not apptainer:
        raise RuntimeError('Apptainer is missing; run sandweave doctor')
    # Bind at the same host paths: helper arguments include node-local sockets.
    result = [apptainer, 'exec', '--userns', '--contain', '--ipc', '--cleanenv', '--no-home',
              '--bind', str(lab) + ':' + str(lab), '--bind', str(local) + ':' + str(local),
              '--pwd', str(lab), str(Path(lab) / 'tools/debian-trixie.sif'),
              'env', 'LD_LIBRARY_PATH=' + str(helpers / 'usr/lib/x86_64-linux-gnu')]
    if memory_limit:
        # Limit the network helper, after Apptainer's Go runtime has started.
        result += [str(helpers / 'usr/bin/prlimit'), '--as=536870912', '--']
    return [*result, str(executable), *arguments]


if __name__ == '__main__':
    if len(sys.argv) < 3:
        raise SystemExit('usage: runtime_tools.py limit COMMAND... | affinity CPUS COMMAND...')
    if sys.argv[1] == 'limit':
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
        arguments = sys.argv[2:]
    elif sys.argv[1] == 'affinity':
        selected = {int(value) for value in sys.argv[2].split(',')}
        if not selected or not selected <= os.sched_getaffinity(0):
            raise SystemExit('CPU selection exceeds the inherited allocation')
        os.sched_setaffinity(0, selected)
        arguments = sys.argv[3:]
    else:
        raise SystemExit('unknown runtime tool operation')
    if not arguments:
        raise SystemExit('missing command')
    os.execvpe(arguments[0], arguments, os.environ)
