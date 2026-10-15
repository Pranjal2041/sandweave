"""Run against a privately selected GNOME installation; no GPU required."""
import os

import pytest

from sandweave import Sandbox, Memory
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_PROC_AUDIT_DESKTOP'), reason='explicit GNOME worker required')]


def test_gnome_with_proc_visibility_and_audit(tmp_path):
    env = Sandbox(template='gnome', hidepid=2, audit=True, memory=Memory('4GiB','1GiB'))
    try:
        assert 'hidepid=2' in env.run('cat /proc/mounts').stdout
        image = env.desktop.screenshot()
        assert image.size == (1920,1080)
        image.save(tmp_path/'gnome.png')
        report = env.audit_log(tmp_path/'gnome.jsonl')
        assert report['complete'] and report['dropped'] == 0
    finally:
        env.terminate()
        env.close()
        connection = local_connection()
        connection.call('_shutdown_if_idle')
        connection.close()
