"""CLI sign-in through a real SSH tunnel to a disposable remote controller."""
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import uuid

import pytest

from sandweave import Cluster

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.environ.get('SANDWEAVE_DASHBOARD_SSH'),
    reason='explicit SSH test host required')]


def test_dashboard_cli_reaches_remote_controller_over_ssh(monkeypatch):
    from playwright.sync_api import sync_playwright, expect
    import sandweave
    root=Path(os.environ.get('SANDWEAVE_DASHBOARD_REMOTE_ARTIFACTS','runs/dashboard-ssh')).resolve()/uuid.uuid4().hex
    root.mkdir(parents=True)
    host=os.environ['SANDWEAVE_DASHBOARD_SSH']
    source=str(Path(sandweave.__file__).resolve().parent.parent)
    monkeypatch.setenv('SANDWEAVE_HOME',str(root/'client'))
    script=f'from sandweave import Cluster\nc=Cluster.start("dashboard-remote", directory={str(root / "controller")!r}, local_worker=False)\nprint(c.info["id"])\nc.close()\n'
    command=shlex.join(['env','PYTHONPATH='+source,'SANDWEAVE_HOME='+str(root/'remote-client'),str(Path(sys.executable).absolute()),'-'])
    subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10',host,command],
                   input=script,text=True,check=True,capture_output=True,timeout=60)
    uri='ssh://'+host+str(root/'controller')
    cli=None
    try:
        cli=subprocess.Popen([sys.executable,'-m','sandweave.cli','dashboard',uri,'--no-open'],
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        url=cli.stdout.readline().strip()
        assert url.startswith('http://127.0.0.1:') and '#ticket=' in url
        with sync_playwright() as tools:
            browser=tools.chromium.launch(headless=True)
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.goto(url)
            expect(page.locator('#page-title')).to_have_text('Cluster overview')
            expect(page.locator('#overview-content')).to_contain_text('No workers connected')
            assert cli.poll() is None
            page.locator('[data-nav="logs"]').click()
            expect(page.locator('#controller-health')).to_contain_text(host.split('@')[-1])
            page.screenshot(path=str(root/'remote-controller.png'),full_page=True)
            browser.close()
    finally:
        if cli is not None:
            cli.send_signal(signal.SIGINT)
            cli.communicate(timeout=20)
        with Cluster.connect(uri) as cluster:
            cluster.stop()
