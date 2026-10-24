"""GNOME desktops with guest clocks: start dates, frozen pauses and memory restores."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import time

import pytest

from sandweave import Memory, Sandbox
from test_guest_clock_live import SLACK, assert_offset, probe, record

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit isolated worker required')]
MEMORY = Memory('4GiB', '1GiB')


def screenshot(env, name):
    directory = os.environ.get('SANDWEAVE_CLOCK_SCREENSHOTS')
    image = env.desktop.screenshot()
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        image.save(Path(directory) / (name + '.png'))
    return image


def test_desktop_start_date_freeze_and_memory_restore():
    start = datetime(2025, 3, 1, 9, 0, tzinfo=timezone.utc)
    with Sandbox(template='gnome', clock_start=start, memory=MEMORY) as env:
        reading = probe(env)
        elapsed = reading['syscall_realtime'] - start.timestamp()
        assert 0 <= elapsed < env.timings['ready_seconds'] + 120
        assert env.run('date -u +%F', check=True).stdout.strip() == '2025-03-01'
        first = screenshot(env, '1-start-date')
        assert first.size == (1920, 1080)
        before_pause = probe(env)
        env.pause()
        time.sleep(30)
        env.resume()
        after_pause = probe(env)
        host = after_pause['host_before'] - before_pause['host_after']
        advanced = after_pause['syscall_realtime'] - before_pause['syscall_realtime']
        assert host >= 30 and advanced < host - 30 + SLACK
        env.desktop.keyboard.press('super')
        screenshot(env, '2-after-frozen-pause')
        env.desktop.keyboard.press('Escape')
        checkpoint = env.snapshot(state='memory')
        assert checkpoint.verify()['status'] == 'passed'
    # The top bar shows month, day and minutes, so all of them must move.
    offset = timedelta(days=100, hours=5, minutes=30).total_seconds()
    with Sandbox(snapshot=checkpoint, clock_offset=offset, memory=MEMORY) as restored:
        assert_offset(probe(restored), offset)
        expected = (datetime.now(timezone.utc) + timedelta(seconds=offset)).strftime('%F')
        assert restored.run('date -u +%F', check=True).stdout.strip() == expected
        # The shell refreshes its clock on its next minute tick.
        time.sleep(65)
        screenshot(restored, '3-memory-restore-new-offset')
        observation = restored.desktop.step({'mouse': {'move': [960, 540]}})
        assert observation.image.size == (1920, 1080)
        restored.desktop.keyboard.press('super')
        time.sleep(1)
        screenshot(restored, '4-restored-overview')
        record('desktop', {'start_elapsed_seconds': elapsed, 'pause_host_seconds': host,
                           'pause_guest_seconds': advanced, 'restore_offset_seconds': offset})
