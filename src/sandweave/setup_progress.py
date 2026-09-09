"""Terminal progress and complete logs for installation work."""
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid


def plain(value):
    """Treat subprocess output as text, never terminal commands or Rich markup."""
    value = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', str(value))
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    return ''.join(c for c in value if c.isprintable() or c in '\n\r\t')


def duration(seconds):
    seconds = int(seconds)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return (f'{hours}h {minutes:02}m' if hours else
            f'{minutes}m {seconds:02}s' if minutes else f'{seconds}s')


def size(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}' if unit != 'B' else f'{value:.0f} B'
        value /= 1024


class Stage:
    """One measured task; unknown totals pulse instead of claiming a percentage."""
    def __init__(self, label, *, total=None, unit=None, detail='', log=None):
        self.label, self.detail = plain(label), plain(detail)
        self.total, self.unit, self.completed = total, unit, 0
        self.log = log
        self.started = self.updated = self.reported = time.monotonic()
        self.display = None
        self.stream = sys.stderr
        self.interactive = self.stream.isatty() and os.environ.get('TERM') != 'dumb'

    def __enter__(self):
        self.started = self.updated = self.reported = time.monotonic()
        if self.interactive:
            from rich.console import Console
            from rich.progress import Progress, ProgressColumn, BarColumn, TextColumn
            from rich.spinner import Spinner
            from rich.text import Text
            stage = self

            class Running(ProgressColumn):
                def __init__(self):
                    super().__init__()
                    self.spinner = Spinner('dots', style='cyan')

                def render(self, task):
                    # A full download still needs flushing and verification.
                    return self.spinner.render(time.monotonic())

            class Elapsed(ProgressColumn):
                def render(self, task):
                    return Text(duration(time.monotonic() - stage.started))

            class Display(Progress):
                def get_renderables(self):
                    yield Text(stage.label, style='bold', overflow='ellipsis', no_wrap=True)
                    yield self.make_tasks_table(self.tasks)
                    yield Text(stage.detail or 'Waiting for output', style='dim', overflow='ellipsis', no_wrap=True)
                    quiet = time.monotonic() - stage.updated
                    if quiet >= 10:
                        yield Text('No new output for ' + duration(quiet), style='dim')

            self.display = Display(Running(), BarColumn(bar_width=None),
                TextColumn('{task.fields[amount]}', markup=False), Elapsed(),
                console=Console(file=self.stream, highlight=False, markup=False),
                expand=True, transient=True, refresh_per_second=4)
            self.task = self.display.add_task('', total=self.total, amount=self.amount())
            self.display.start()
        else:
            suffix = ' · Log: ' + str(self.log) if self.log else ''
            print(self.label + '...' + suffix, file=self.stream, flush=True)
        return self

    def amount(self):
        if self.unit == 'bytes':
            amount = size(self.completed)
            if self.total:
                amount += ' / ' + size(self.total)
        elif self.total is not None:
            amount = f'{self.completed}/{self.total} {self.unit or "items"}'
        else:
            return ''
        if self.total:
            amount += f'  {min(100, int(100 * self.completed / self.total))}%'
        return amount

    def activity(self):
        quiet = time.monotonic() - self.updated
        detail = self.detail or 'Waiting for output'
        if quiet >= 10:
            detail += ' · No new output for ' + duration(quiet)
        return detail

    def update(self, *, completed=None, advance=0, total=None, detail=None):
        if completed is not None:
            self.completed = completed
        self.completed += advance
        if total is not None:
            self.total = total
        if detail is not None:
            self.detail = plain(detail).strip()[-2000:]
        if completed is not None or advance or detail is not None:
            self.updated = time.monotonic()
        if self.display:
            self.display.update(self.task, completed=self.completed, total=self.total, amount=self.amount())
        elif time.monotonic() - self.reported >= 10:
            self.reported = time.monotonic()
            print(f'{self.label} · {duration(self.reported - self.started)} · '
                  f'{self.amount()} {self.activity()}'.rstrip(), file=self.stream, flush=True)

    def __exit__(self, kind, error, traceback):
        if self.display:
            self.display.stop()
        status = 'Done' if kind is None else 'Interrupted' if issubclass(kind, KeyboardInterrupt) else 'Failed'
        print(f'{status}: {self.label} · {duration(time.monotonic() - self.started)}',
              file=self.stream, flush=True)


def run_logged(command, directory, *, label, env=None, cwd=None):
    """Keep child output on disk; polling a bounded tail cannot block its writes."""
    logs = Path(directory) / 'logs/setup'
    logs.mkdir(parents=True, exist_ok=True)
    name = re.sub(r'[^a-zA-Z0-9 ._-]', '_', label)
    log = logs / (name + '-' + uuid.uuid4().hex[:8] + '.log')
    with log.open('wb') as output, Stage(label, log=log) as stage:
        log.chmod(0o600)
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            with log.open('rb') as tail:
                while True:
                    try:
                        code = process.wait(timeout=.25)
                    except subprocess.TimeoutExpired:
                        code = None
                    # Large compiler logs must not be read from the start on
                    # each refresh, especially on a network filesystem.
                    end = os.fstat(tail.fileno()).st_size
                    tail.seek(max(tail.tell(), end - 8192))
                    chunk = tail.read(8192)
                    lines = plain(chunk.decode(errors='replace')).replace('\r', '\n').splitlines()
                    latest = next((line.strip() for line in reversed(lines) if line.strip()), None)
                    stage.update(detail=latest)
                    if code is not None:
                        break
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise
        if code:
            with log.open('rb') as tail:
                tail.seek(max(0, log.stat().st_size - 6000))
                detail = plain(tail.read().decode(errors='replace'))
            raise ValueError(label + ' failed. Log: ' + str(log) + '\n' + detail)
    return log
