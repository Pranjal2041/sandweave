import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from sandweave.bootstrap import download
from sandweave import setup_progress


def test_logged_build_reports_activity_without_losing_large_output(tmp_path, monkeypatch, capsys):
    observations = []
    update = setup_progress.Stage.update

    def observe(self, **kwargs):
        observations.append(kwargs.get('detail'))
        return update(self, **kwargs)

    monkeypatch.setattr(setup_progress.Stage, 'update', observe)
    script = ("import os,sys,time; print(os.getcwd(), flush=True); "
              "print(os.environ['SANDWEAVE_TEST_VALUE'], flush=True); "
              "sys.stdout.write('x'*2_000_000+'\\n\\x1b[32mExtracting image\\x1b[0m\\r'); "
              "sys.stdout.flush(); time.sleep(.6); print('Image ready', flush=True)")
    log = setup_progress.run_logged([sys.executable, '-c', script], tmp_path,
        label='Build fixture', cwd=tmp_path, env={**os.environ, 'SANDWEAVE_TEST_VALUE': 'test-value'})
    assert log.read_text().startswith(str(tmp_path) + '\ntest-value\n' + 'x' * 2_000_000)
    assert log.read_bytes().endswith(b'\x1b[32mExtracting image\x1b[0m\rImage ready\n')
    assert 'Extracting image' in observations and 'Image ready' in observations
    assert log.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr()
    assert output.out == '' and '\x1b' not in output.err
    assert 'Done: Build fixture' in output.err


def test_failed_build_keeps_log_and_reports_failure(tmp_path, capsys):
    with pytest.raises(ValueError) as caught:
        setup_progress.run_logged([sys.executable, '-c',
            "import sys; print('\\x1b[31mcompiler failed\\x1b[0m'); sys.exit(7)"],
            tmp_path, label='Compile fixture')
    assert 'compiler failed' in str(caught.value)
    assert '\x1b' not in str(caught.value)
    assert str(next((tmp_path / 'logs/setup').iterdir())) in str(caught.value)
    output = capsys.readouterr().err
    assert 'Failed: Compile fixture' in output and 'Done:' not in output


def test_interrupt_releases_owned_build_processes(tmp_path):
    # Run the real runner in another process so SIGINT follows the CLI path.
    child_script = ("import os,time; from pathlib import Path; "
                    f"Path({str(tmp_path / 'child.pid')!r}).write_text(str(os.getpid())); "
                    "time.sleep(60)")
    runner_script = ("from sandweave.setup_progress import run_logged; import sys; "
                     f"run_logged([sys.executable, '-c', {child_script!r}], {str(tmp_path)!r}, label='Interrupt fixture')")
    log = tmp_path / 'runner.log'
    with log.open('wb') as output:
        runner = subprocess.Popen([sys.executable, '-c', runner_script], stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / 'child.pid').exists():
                assert runner.poll() is None and time.monotonic() < deadline
                time.sleep(.02)
            child = int((tmp_path / 'child.pid').read_text())
            os.kill(runner.pid, signal.SIGINT)
            assert runner.wait(timeout=10) != 0
            assert not Path(f'/proc/{child}').exists()
            assert 'Interrupted: Interrupt fixture' in log.read_text()
        finally:
            if runner.poll() is None:
                runner.kill()
                runner.wait()


@pytest.fixture
def download_server():
    payload = b'checked download bytes' * 30_000

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            if self.path != '/unknown':
                self.send_header('Content-Length', len(payload) + (100 if self.path == '/short' else 0))
            self.end_headers()
            if self.path == '/slow':
                for start in range(0, len(payload), 32 * 1024):
                    self.wfile.write(payload[start:start + 32 * 1024])
                    self.wfile.flush()
                    time.sleep(.04)
            else:
                self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('route', ['known', 'unknown'])
def test_download_reports_real_bytes_and_keeps_stdout_clean(download_server, tmp_path, monkeypatch, capsys, route):
    url, payload = download_server
    samples = []
    update = setup_progress.Stage.update

    def observe(self, **kwargs):
        result = update(self, **kwargs)
        samples.append((self.completed, self.total, self.amount()))
        return result

    monkeypatch.setattr(setup_progress.Stage, 'update', observe)
    path = download(url + '/' + route, tmp_path, 'payload.bin', sha256=hashlib.sha256(payload).hexdigest())
    assert path.read_bytes() == payload
    assert samples[-1][0] == len(payload)
    assert all(total == (len(payload) if route == 'known' else None) for _, total, _ in samples)
    assert ('%' in samples[-1][2]) == (route == 'known')
    output = capsys.readouterr()
    assert output.out == '' and '\x1b' not in output.err
    assert 'Done: Download payload.bin' in output.err


@pytest.mark.parametrize('failure', ['short', 'checksum'])
def test_bad_download_does_not_replace_existing_file(download_server, tmp_path, capsys, failure):
    url, _ = download_server
    original = tmp_path / 'payload.bin'
    original.write_bytes(b'existing user file')
    with pytest.raises(ValueError, match='Incomplete download|checksum mismatch'):
        download(url + ('/short' if failure == 'short' else '/known'), tmp_path,
                 original.name, sha256='0' * 64)
    assert original.read_bytes() == b'existing user file'
    assert list(tmp_path.iterdir()) == [original]
    output = capsys.readouterr().err
    assert 'Failed:' in output and 'Done:' not in output


@pytest.mark.parametrize('columns', [100, 44])
def test_actual_terminal_download_and_quiet_build(download_server, tmp_path, columns):
    """Capture the actual ANSI display, including quiet time and narrow wrapping."""
    import codecs
    import errno
    import fcntl
    import json
    import pty
    import select
    import struct
    import termios
    pyte = pytest.importorskip('pyte')
    url, payload = download_server
    # Controlled build output exercises the real subprocess runner without
    # downloading a compiler or changing an existing sandbox installation.
    build = ("import time; print('Computing main repo mapping: [dependencies]', flush=True); "
             "time.sleep(11.5); print('Build finished', flush=True)")
    script = ("from sandweave.bootstrap import download; from sandweave.setup_progress import run_logged; "
              "import sys; print('Sandweave setup · progress fixture', flush=True); "
              f"download({url + '/slow'!r}, {str(tmp_path)!r}, 'fixture.tar', sha256={hashlib.sha256(payload).hexdigest()!r}); "
              f"run_logged([sys.executable, '-c', {build!r}], {str(tmp_path)!r}, label='Build gVisor')")
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 16, columns, 0, 0))
    child = subprocess.Popen([sys.executable, '-c', script], stdin=slave, stdout=slave, stderr=slave,
        start_new_session=True, env={**os.environ, 'TERM': 'xterm-256color',
        'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
    os.close(slave)
    screen = pyte.Screen(columns, 16)
    stream = pyte.Stream(screen)
    decoder = codecs.getincrementaldecoder('utf-8')('replace')
    recording, frames = bytearray(), {}
    try:
        deadline = time.monotonic() + 30
        while True:
            assert time.monotonic() < deadline, 'terminal progress did not finish'
            if not select.select([master], [], [], .1)[0]:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            recording.extend(chunk)
            stream.feed(decoder.decode(chunk))
            text = '\n'.join(screen.display)
            # Save cell colors as well as text for a faithful terminal image.
            cells = [[screen.buffer[y][x]._asdict() for x in range(columns)] for y in range(16)]
            if 'Download fixture.tar' in text and '%' in text and '100%' not in text:
                frames['download'] = {'text': text, 'cells': cells}
            if 'No new output for' in text:
                frames['quiet-build'] = {'text': text, 'cells': cells}
        assert child.wait(timeout=3) == 0
        assert set(frames) == {'download', 'quiet-build'}
        assert '%' not in frames['quiet-build']['text']
        assert 'Done: Build gVisor' in '\n'.join(screen.display)
        assert not screen.cursor.hidden
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGINT)
            child.wait(timeout=20)
        os.close(master)
        output = Path(os.environ.get('SANDWEAVE_PROGRESS_ARTIFACTS', tmp_path))
        output.mkdir(parents=True, exist_ok=True)
        (output / f'terminal-{columns}.ansi').write_bytes(recording)
        (output / f'terminal-{columns}.json').write_text(json.dumps(frames, indent=2) + '\n')
