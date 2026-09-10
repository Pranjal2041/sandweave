"""Real browser acceptance against explicitly seeded monitoring fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from test_dashboard import controller, seed, dashboard_server

pytestmark=pytest.mark.skipif(not os.environ.get('SANDWEAVE_BROWSER_TESTS'),reason='explicit browser acceptance required')


def test_default_start_prints_a_reusable_browser_link(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright, expect
    from sandweave import Cluster
    from sandweave.weave.transport import address
    monkeypatch.setenv('SANDWEAVE_HOME', str(tmp_path / 'master'))
    output = subprocess.check_output([sys.executable, '-m', 'sandweave.cli',
        'cluster', 'start', 'browser-link', '--no-worker'], cwd=tmp_path, text=True)
    with Cluster.connect('browser-link') as cluster:
        try:
            link = next(line.removeprefix('Dashboard: ') for line in output.splitlines()
                        if line.startswith('Dashboard: '))
            token = address(link)['token']
            root = Path(os.environ['SANDWEAVE_BROWSER_TESTS'])
            root.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as browser_tools:
                browser = browser_tools.chromium.launch(headless=True)
                try:
                    for attempt in range(2):
                        # Each fresh browser has no saved session or token.
                        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                        page = context.new_page()
                        urls, errors = [], []
                        page.on('request', lambda request: urls.append(request.url))
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.goto(link)
                        expect(page.locator('#page-title')).to_have_text('Cluster overview')
                        expect(page.locator('#connection-status')).to_have_text('Live')
                        assert 'token=' not in page.url
                        assert all(token not in url for url in urls)
                        assert page.evaluate('Object.keys(localStorage).length') == 0
                        assert page.evaluate('document.cookie') == ''
                        page.screenshot(path=str(root / ('startup-dashboard-' + str(attempt) + '.png')), full_page=True)
                        page.reload()
                        expect(page.locator('#page-title')).to_have_text('Cluster overview')
                        page.locator('#logout').click()
                        expect(page.locator('#login')).to_be_visible()
                        assert not errors
                        context.close()
                finally:
                    browser.close()
        finally:
            cluster.stop()


def test_browser_navigation_filters_logs_updates_and_mobile(controller):
    from playwright.sync_api import sync_playwright, expect
    seed(controller,count=64)
    worker=controller.state.get('worker','worker-2')
    worker['gpus']=[{'uuid':'GPU-fixture','model':'NVIDIA L40S','index':0}]
    worker['capacity']['gpu']=1
    worker['inventory']['telemetry']['gpus']=[dict(uuid='GPU-fixture',utilization_percent=62,
        memory_used_bytes=8*1024**3,memory_total_bytes=48*1024**3,temperature_c=56,power_watts=190)]
    controller.state.put('worker',worker)
    root=Path(os.environ['SANDWEAVE_BROWSER_TESTS']);root.mkdir(parents=True,exist_ok=True)
    with dashboard_server(controller) as (dashboard,url),sync_playwright() as browser_tools:
        now=time.time()
        for i in range(90):
            data={'cpu_cores':3+i%8*.45,'rss_bytes':(4+i%10*.2)*1024**3,'sandboxes':64,'pending':0}
            dashboard.monitor.db.execute('INSERT INTO samples(time,kind,id,data) VALUES (?,?,?,?)',
                (now-3600+i*40,'cluster','cluster',json.dumps(data)))
        browser=browser_tools.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1120},device_scale_factor=1)
        errors=[];csp=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('console',lambda message:csp.append(message.text) if 'Content Security Policy' in message.text else None)
        page.goto(url+'/dashboard/#ticket='+dashboard.ticket()['ticket'])
        expect(page.locator('#page-title')).to_have_text('Cluster overview')
        expect(page.locator('.card').first).to_contain_text('64')
        assert 'ticket=' not in page.url
        assert page.evaluate('Object.keys(localStorage).length')==0
        assert page.evaluate('document.cookie')==''
        page.screenshot(path=str(root/'overview.png'),full_page=True)
        page.locator('[data-nav="gpus"]').click()
        expect(page.locator('#resource-results')).to_contain_text('NVIDIA L40S')
        page.locator('#resource-results [data-kind="gpus"]').click()
        expect(page.locator('#detail-fields')).to_contain_text('62%')
        page.locator('#detail-range').select_option('900')
        expect(page.locator('#detail-history .dot.single')).to_be_visible()
        page.screenshot(path=str(root/'gpu.png'),full_page=True)
        page.get_by_role('button',name='Close resource details').click()
        page.locator('[data-nav="workers"]').click()
        expect(page.locator('#resource-results')).to_contain_text('atlas-01')
        page.locator('#resource-results [data-id="worker-0"]').click()
        expect(page.locator('#detail-content')).to_contain_text('Host memory available')
        page.screenshot(path=str(root/'worker.png'),full_page=True)
        page.get_by_role('button',name="View this worker's sandboxes →").click()
        expect(page.locator('#resource-results')).to_contain_text('22 results')
        page.get_by_role('button',name='Clear',exact=True).click()
        expect(page.locator('#resource-results')).to_contain_text('64 results')
        page.get_by_role('button',name='Next',exact=True).click()
        expect(page.locator('#resource-results')).to_contain_text('51–64')
        page.locator('#search').fill('eval-23')
        expect(page.locator('#resource-results')).to_contain_text('1 results')
        page.locator('[data-nav="jobs"]').click()
        expect(page.locator('#resource-results')).to_contain_text('job-example')
        page.locator('#resource-results [data-id="job-example"]').click()
        expect(page.locator('#detail-content')).to_contain_text('Task 0')
        page.locator('#detail-content [data-id="task-example"]').click()
        expect(page.locator('#detail-output')).to_have_text('completed\n')
        page.get_by_role('button',name='stderr',exact=True).click()
        expect(page.locator('#detail-output')).to_have_text('warning example\n')
        page.locator('#attempt-select').select_option('task-example-0')
        expect(page.locator('#detail-output')).to_have_text('retry requested\n')
        page.screenshot(path=str(root/'task-output.png'),full_page=True)
        page.reload()
        expect(page.locator('#detail-output')).to_have_text('completed\n')
        page.get_by_role('button',name='Close resource details').click()
        page.locator('[data-nav="overview"]').click()
        expect(page.locator('.card').first).to_contain_text('64')
        sandbox=controller.state.get('allocation','sandbox-0')
        controller.state.put('allocation',{**sandbox,'state':'pending','reason':'worker resources are reserved'})
        dashboard.monitor.refresh()
        expect(page.locator('.card').first).to_contain_text('63',timeout=10000)
        expect(page.locator('.card').nth(2)).to_contain_text('1')
        page.locator('#pause').click()
        expect(page.locator('#connection-status')).to_have_text('Paused')
        page.locator('#pause').click()
        page.route('**/api/overview',lambda route:route.fulfill(status=503,content_type='application/json',body='{"error":"test connection loss"}'))
        page.locator('#refresh').click()
        expect(page.locator('#notice')).to_contain_text('test connection loss')
        expect(page.locator('#connection-status')).to_have_text('Data is stale')
        page.unroute('**/api/overview')
        page.locator('#refresh').click()
        expect(page.locator('#connection-status')).to_have_text('Live')
        page.locator('[data-nav="snapshots"]').click()
        expect(page.locator('#resource-results')).to_contain_text('snap-example')
        page.locator('[data-nav="events"]').click()
        expect(page.locator('#resource-results')).to_contain_text('attempt completed')
        page.get_by_role('button',name='Older',exact=True).click()
        expect(page.locator('#resource-results')).to_contain_text('Earlier events')
        page.locator('[data-nav="logs"]').click()
        expect(page.locator('#controller-output')).to_contain_text('No log has been written yet.')
        expect(page.locator('#controller-health')).to_contain_text('Resident memory')
        # User-supplied names render as text, including in drawer headings.
        w=controller.state.get('worker','worker-0')
        controller.state.put('worker',{**w,'name':'<img src=x onerror="window.PWNED=1">'})
        dashboard.monitor.refresh()
        page.locator('[data-nav="workers"]').click()
        expect(page.locator('#resource-results')).to_contain_text('<img src=x onerror=')
        assert page.evaluate('window.PWNED') is None
        assert page.locator('#resource-results img').count()==0
        page.set_viewport_size({'width':390,'height':844})
        page.locator('[data-nav="overview"]').click()
        expect(page.locator('.card').first).to_contain_text('63')
        page.screenshot(path=str(root/'mobile.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.set_viewport_size({'width':1440,'height':1120})
        page.locator('#logout').click()
        expect(page.locator('#login')).to_be_visible()
        assert not errors,errors
        assert not csp,csp
        browser.close()
