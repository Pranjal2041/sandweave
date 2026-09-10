"""Real worker telemetry and browser output from disposable coding sandboxes."""
import json
import os
from pathlib import Path
import time

import pytest

from sandweave import Sandbox, Job
from test_weave_live import cluster, wait_for

pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.environ.get('SANDWEAVE_WEAVE_INTEGRATION'),
    reason='explicit isolated workers required')]


def test_live_dashboard_measures_cpu_and_memory_and_reads_job_output(cluster):
    from playwright.sync_api import sync_playwright, expect
    root=Path(os.environ['SANDWEAVE_WEAVE_INTEGRATION'])
    with sync_playwright() as tools:
        browser=tools.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1120})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(cluster.dashboard())
        expect(page.locator('#page-title')).to_have_text('Cluster overview')
        with Sandbox(target=cluster,detached=True,name='dashboard-code-check') as env:
            started=time.time()
            process=env.exec("python -u -c 'import time; x=bytearray(48*1024*1024); x[::4096]=b\"x\"*(len(x)//4096); print(\"monitoring live\", flush=True); end=time.monotonic()+30\nwhile time.monotonic()<end: sum(range(40000))'")
            readings=[]
            def measured():
                response=page.request.get(page.url.split('#')[0].split('/dashboard/')[0]+'/dashboard/api/detail',
                                          params={'kind':'sandboxes','identity':env.id})
                if response.status!=200:
                    return False
                value=response.json();metric=value['telemetry']
                if metric.get('time',0)>started+1 and (metric.get('rss_bytes') or 0)>48*1024**2 and (metric.get('cpu_cores') or 0)>0:
                    readings.append(metric)
                    return True
                return False
            wait_for(measured,timeout=30)
            assert readings[-1]['rss_bytes']>48*1024**2
            assert env.run('test ! -e /dev/kvm').returncode==0
            page.locator('#refresh').click()
            expect(page.locator('.card').first).to_contain_text('1')
            page.screenshot(path=str(root/'live-overview.png'),full_page=True)
            page.locator('[data-nav="sandboxes"]').click()
            expect(page.locator('#resource-results')).to_contain_text('dashboard-code-check')
            page.locator('#resource-results [data-id="'+env.id+'"]').click()
            expect(page.locator('#detail-fields')).to_contain_text('Resident memory')
            expect(page.locator('#detail-log-meta')).not_to_have_text('')
            page.screenshot(path=str(root/'live-sandbox.png'),full_page=True)
            page.get_by_role('button',name='Close resource details').click()
            process.wait()
        job=Job.submit("python -u -c 'import sys,time; print(\"live job output\", flush=True); print(\"live stderr output\", file=sys.stderr, flush=True); time.sleep(8); sys.exit(7)'",
                       target=cluster,detached=True)
        try:
            page.locator('[data-nav="jobs"]').click()
            expect(page.locator('#resource-results [data-id="'+job.id+'"]').first).to_be_visible(timeout=120000)
            page.locator('#resource-results [data-id="'+job.id+'"]').click()
            expect(page.locator('#detail-content')).to_contain_text('Task 0')
            page.get_by_role('button',name='Task 0',exact=True).click()
            expect(page.locator('#detail-output')).to_contain_text('live job output',timeout=120000)
            page.get_by_role('button',name='stderr',exact=True).click()
            expect(page.locator('#detail-output')).to_contain_text('live stderr output')
            result=job.result(timeout=120)
            assert result.returncode==7
            expect(page.locator('#detail-fields')).to_contain_text('failed',timeout=15000)
            page.screenshot(path=str(root/'live-job.png'),full_page=True)
        finally:
            job.cancel();job.close()
        (root/'measurements.json').write_text(json.dumps(readings,indent=2))
        assert not errors,errors
        browser.close()
