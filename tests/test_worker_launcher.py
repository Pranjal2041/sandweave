"""Exercise the installed worker's subprocess boundary in a separate process."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


def worker_code(code, *, timeout=30):
    source = Path(__file__).resolve().parents[1] / 'src'
    result = subprocess.run([sys.executable, '-u', '-c',
        'from sandweave.sandbox.launcher import install\ninstall()\n' + textwrap.dedent(code)],
        env={**os.environ, 'PYTHONPATH': str(source)}, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


@pytest.mark.parametrize('code', [
    """
    import subprocess, sys, tempfile
    with tempfile.TemporaryDirectory() as cwd:
        result = subprocess.run([sys.executable, '-c', 'import os,sys; print(os.getcwd()); print(os.getenv("VALUE")); print(sys.stdin.read()); print("error", file=sys.stderr)'],
            input='unicode €', text=True, capture_output=True, cwd=cwd, env={'VALUE': 'a $value'})
        assert result.stdout == cwd + '\\na $value\\nunicode €\\n'
        assert result.stderr == 'error\\n'
    result = subprocess.run('printf out; printf err >&2; exit 7', shell=True, capture_output=True)
    assert (result.returncode, result.stdout, result.stderr) == (7, b'out', b'err')
    result = subprocess.run('[[ -n "$BASH_VERSION" ]] && printf bash', shell=True, executable='/bin/bash', capture_output=True)
    assert result.returncode == 0 and result.stdout == b'bash'
    """,
    """
    import subprocess, sys
    with subprocess.Popen([sys.executable, '-c', 'import sys; print("ready",flush=True); print(sys.stdin.read(),end="")'],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as child:
        assert child.stdout.readline() == 'ready\\n'
        output, error = child.communicate('hello €')
        assert output == 'hello €' and error is None
        assert child.wait() == child.poll() == 0
    """,
    """
    import subprocess, time
    with subprocess.Popen(['sleep', '10']) as child:
        try:
            child.wait(timeout=.02)
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError('missing timeout')
        child.terminate()
        assert child.wait(timeout=2) == -15
    started = time.monotonic()
    try:
        subprocess.run(['sleep', '10'], timeout=.02)
    except subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError('missing run timeout')
    assert time.monotonic() - started < 2
    """,
    """
    import subprocess, os, sys, tempfile
    with tempfile.TemporaryFile() as original:
        os.dup2(original.fileno(), 200)
        try:
            child = subprocess.run([sys.executable, '-c', 'import os; os.write(200,b"preserved")'], pass_fds=(200,), check=True)
            original.seek(0)
            assert original.read() == b'preserved'
        finally:
            os.close(200)
    with tempfile.TemporaryFile() as output:
        subprocess.run(['printf', 'file output'], stdout=output, check=True)
        output.seek(0)
        assert output.read() == b'file output'
    """,
    """
    import subprocess, os, sys, json
    result = subprocess.run([sys.executable, '-c', 'import os,json; print(json.dumps([os.getpid(),os.getsid(0),os.getuid(),os.getgid(),os.umask(0),sorted(os.sched_getaffinity(0))]))'],
                            start_new_session=True, umask=0o027, user=os.getuid(), group=os.getgid(), capture_output=True, text=True, check=True)
    pid, session, uid, gid, mask, affinity = json.loads(result.stdout)
    assert pid == session and uid == os.getuid() and gid == os.getgid() and mask == 0o027
    assert affinity == sorted(os.sched_getaffinity(0))
    """,
    """
    import subprocess, errno, tempfile, os
    try:
        subprocess.run(['/no/such/worker-command'])
    except FileNotFoundError as error:
        assert error.errno == errno.ENOENT
    else:
        raise AssertionError('missing exec error')
    with tempfile.NamedTemporaryFile() as file:
        try:
            subprocess.run([file.name])
        except PermissionError as error:
            assert error.errno == errno.EACCES
        else:
            raise AssertionError('missing permission error')
    assert subprocess.run(['/bin/true']).returncode == 0
    """,
    """
    import subprocess, sys, json, os, time
    from pathlib import Path
    from concurrent.futures import ThreadPoolExecutor
    from sandweave.sandbox.launcher import _server
    before = len(list(Path('/proc/self/fd').iterdir()))
    def run(index):
        result = subprocess.run([sys.executable, '-c', 'import sys; print(sys.argv[1])', str(index)], capture_output=True, text=True, check=True)
        assert result.stdout == str(index) + '\\n'
    with ThreadPoolExecutor(32) as executor:
        list(executor.map(run, range(128)))
    assert len(list(Path('/proc/self/fd').iterdir())) <= before + 1
    deadline = time.monotonic() + 5
    children = Path(f'/proc/{_server.process.pid}/task/{_server.process.pid}/children')
    while children.read_text().strip() and time.monotonic() < deadline:
        time.sleep(.01)
    assert not children.read_text().strip()
    """,
    """
    import subprocess, tempfile, os
    from pathlib import Path
    from sandweave.sandbox.launcher import _server
    with tempfile.NamedTemporaryFile() as image:
        def image_handles(pid):
            return [p for p in Path(f'/proc/{pid}/fd').iterdir() if p.exists() and os.path.samefile(p, image.name)]
        assert image_handles(os.getpid())
        assert not image_handles(_server.process.pid)
        with subprocess.Popen(['sleep', '10']) as child:
            assert not image_handles(child.pid)
            child.kill()
    """,
    """
    import subprocess, os, sys, tempfile, json
    os.environ['AFTER_LAUNCHER_START'] = 'present'
    eligible = sorted(os.sched_getaffinity(0))
    os.sched_setaffinity(0, eligible[:1])
    with tempfile.TemporaryDirectory() as directory:
        os.chdir(directory)
        previous = os.umask(0o037)
        result = subprocess.run([sys.executable, '-c', 'import os,json; print(json.dumps([os.getcwd(),os.environ["AFTER_LAUNCHER_START"],os.umask(0),sorted(os.sched_getaffinity(0)),os.getpgrp()]))'], capture_output=True, text=True, check=True)
        os.umask(previous)
        assert json.loads(result.stdout) == [directory, 'present', 0o037, eligible[:1], os.getpgrp()]
    """,
    """
    import subprocess, time
    from concurrent.futures import ThreadPoolExecutor
    with subprocess.Popen(['sleep', '10']) as child, ThreadPoolExecutor(1) as executor:
        waiting = executor.submit(child.wait)
        time.sleep(.02)
        child.kill()
        assert waiting.result(timeout=2) == -9
    """,
    """
    import subprocess, os, resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < 2048:
        resource.setrlimit(resource.RLIMIT_NOFILE, (2048, hard))
    descriptors = [os.open('/dev/null', os.O_RDONLY) for _ in range(1100)]
    try:
        assert descriptors[-1] > 1024
        result = subprocess.run(['printf', 'many open files'], capture_output=True, text=True, check=True)
        assert result.stdout == 'many open files'
    finally:
        for fd in descriptors:
            os.close(fd)
    """,
    """
    import subprocess, os, resource, signal, sys, json
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1, signal.SIGCHLD})
    soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
    resource.setrlimit(resource.RLIMIT_CORE, (0, hard))
    result = subprocess.run([sys.executable, '-c', 'import signal,resource,json; print(json.dumps([sorted(signal.pthread_sigmask(signal.SIG_BLOCK, set())),resource.getrlimit(resource.RLIMIT_CORE)[0]]))'], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == [sorted([signal.SIGUSR1, signal.SIGCHLD]), 0]
    """,
    """
    import subprocess, resource
    from sandweave.sandbox.launcher import _server
    soft, hard = resource.prlimit(_server.process.pid, resource.RLIMIT_NOFILE)
    resource.prlimit(_server.process.pid, resource.RLIMIT_NOFILE, (64, hard))
    children = []
    rejected = False
    try:
        for _ in range(80):
            try:
                children.append(subprocess.Popen(['/bin/sleep', '10'], stdin=subprocess.DEVNULL,
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            except (OSError, ChildProcessError):
                rejected = True
                break
        assert rejected, 'descriptor pressure was not exercised'
        assert _server.process.poll() is None, 'resource pressure killed the launcher'
    finally:
        for child in children:
            child.kill()
        for child in children:
            child.wait(timeout=5)
    assert subprocess.run(['/bin/true'], check=True).returncode == 0
    """,
])
def test_worker_subprocess_contract(code):
    worker_code(code)
