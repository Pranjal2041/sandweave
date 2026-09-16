"""A Dockerfile build can be the first operation in an empty installation."""
import os

import pytest

from sandweave import Sandbox
from sandweave.templates.build import build

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable installation required')]


def test_build_before_setup_and_reuse(tmp_path, monkeypatch):
    installation = tmp_path / 'installation'
    monkeypatch.setenv('SANDWEAVE_HOME', str(installation))
    monkeypatch.delenv('SANDWEAVE_ASSETS', raising=False)
    context = tmp_path / 'context'
    context.mkdir()
    (context / 'Dockerfile').write_text('FROM alpine:3.22\nRUN echo first-use > /built\n')
    assert not (installation / 'config.json').exists()
    first = second = None
    try:
        first = build(context)
        assert (installation / 'config.json').is_file()
        with Sandbox(cache=first) as env:
            assert env.run('cat /built', check=True).stdout == 'first-use\n'
        second = build(context)
        assert second.id == first.id
    finally:
        for reference in (first, second):
            if reference is not None:
                reference._connection.close()
        if (installation / 'config.json').is_file():
            from sandweave.sandbox.targets import connect
            connection = connect()
            try:
                connection.call('_shutdown_if_idle')
            finally:
                connection.close()
