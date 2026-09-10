"""Build desktop software from a coding base and use the exported runtime."""
import os
from pathlib import Path

import pytest

from sandweave import Sandbox
from sandweave.bootstrap import Builder
from sandweave.installation import publish
from sandweave.sandbox.targets import local_connection
from sandweave.templates.resolve import Template

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_BUILD_DESKTOP'), reason='explicit desktop build acceptance required')]


def test_desktop_image_and_helpers_export(tmp_path, monkeypatch):
    base = Path(os.environ['SANDWEAVE_TRANSFER_ASSETS'])
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path))
    monkeypatch.delenv('SANDWEAVE_ASSETS', raising=False)
    before = tmp_path.stat()
    root = Builder(tmp_path).build('gnome', Template('gnome').resolve(), base=base)
    helper = root / 'tools/fast-io/bridge'
    assert helper.read_bytes().startswith(b'\x7fELF')
    assert helper.stat().st_uid == os.getuid() and helper.stat().st_mode & 0o111
    assert (root / 'tools/fast-io/libxcb-xtest.so.0').is_file()
    assert tmp_path.stat().st_gid == before.st_gid
    publish(tmp_path, root, template='gnome')
    try:
        with Sandbox(template='gnome') as env:
            assert env.run('gnome-shell --version', check=True).stdout.startswith('GNOME Shell')
            assert env.run("stat -c '%a' /tmp", check=True).stdout.strip() == '1777'
            assert env.info['vnc']['port'] > 0
            assert env.desktop.screenshot().size == (1920, 1080)
    finally:
        connection = local_connection()
        connection.call('_shutdown_if_idle')
        connection.close()
