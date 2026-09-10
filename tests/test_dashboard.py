"""Monitoring correctness, bounded data, and browser authorization boundaries."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest

from sandweave import Cluster
from sandweave.sandbox.sandbox import definition
from sandweave.sandbox.telemetry import Sampler, members, process_table, tail
from sandweave.weave.controller import Controller
from sandweave.weave.dashboard import Dashboard
from sandweave.weave.monitor import Monitor
from sandweave.sandbox.wire import encode, decode


@pytest.fixture
def controller(tmp_path):
    value = Controller(tmp_path / 'controller')
    yield value
    value.close()


def seed(controller, *, count=8):
    """Explicit fixture data for views; these records never launch a sandbox."""
    now = time.time()
    request = definition(detached=True)
    for i in range(3):
        metric = dict(time=now, cpu_busy_percent=23+i*12, sandbox_rss_bytes=(i+1)*1024**3,
            host_memory_total_bytes=64*1024**3, host_memory_available_bytes=38*1024**3,
            disk_total_bytes=1024**4, disk_available_bytes=700*1024**3,
            network_receive_bytes_per_second=2*1024**2, network_send_bytes_per_second=500*1024,
            gpus=[], sandboxes={})
        controller.state.put('worker', dict(id='worker-'+str(i), name=['atlas-01','atlas-02','render-01'][i],
            state='ready', capacity=dict(slots=16, cpus=8, memory=32*1024**3, gpu=0),
            external=dict(slots=1, memory=2*1024**3,gpu=0), labels={'region':'west','team':'training'},
            cpu_ids=list(range(i*8,(i+1)*8)), gpus=[], draining=i==2, seen=now,
            inventory={'hostname':'host-'+str(i),'telemetry':metric}, endpoint={'token':'DO-NOT-EXPOSE-WORKER-TOKEN'}),
            event={'message':'worker registered'})
    for i in range(2):
        controller.state.put('pool',dict(id='pool-'+str(i),name=['coding-evals','desktop-agents'][i],
            state='ready',desired='running',request=request,size=24,warm=4,weight=i+1,priority=0,
            labels={'team':'training'},placement='spread',baseline='snap-fixture'),event={'message':'pool requested'})
    for i in range(count):
        spec={**request['spec'],'name':'eval-'+str(i),'setup':[{'secret':'DO-NOT-EXPOSE-SETUP'}]}
        controller.state.put('allocation',dict(id='sandbox-'+str(i),parent='pool-'+str(i%2),worker='worker-'+str(i%3),
            state='leased' if i%3 else 'ready',spec=spec,desired='running',released=False,lease='lease-'+str(i) if i%3 else None,
            info={'agent':{'token':'DO-NOT-EXPOSE-AGENT-TOKEN'},'timings':{'ready_seconds':.22+i*.03}}),
            event={'message':'worker acknowledged creation','state':'ready'})
        with controller.state.transaction():
            worker=controller.state.get('worker','worker-'+str(i%3))
            worker['inventory']['telemetry']['sandboxes']['sandbox-'+str(i)]={'time':now,'cpu_cores':.2+i*.02,'rss_bytes':120*1024**2,'processes':5}
            controller.state.put('worker',worker)
    controller.state.put('job',dict(id='job-example',state='running',pool='pool-0',
        request={'files':{'secret.py':b'DO-NOT-EXPOSE-PROGRAM'}}),event={'message':'job submitted'})
    controller.state.put('task',dict(id='task-example',parent='job-example',index=0,state='succeeded',attempt=1,
        sandbox='sandbox-0',result={'stdout':b'completed\n','stderr':b'warning example\n','returncode':0}),
        event={'message':'attempt completed','returncode':0})
    controller.state.put('attempt',dict(id='task-example-0',parent='task-example',result={
        'stdout':b'previous attempt\n','stderr':b'retry requested\n','returncode':75}))
    controller.state.put('artifact',dict(id='snap-example',info={'kind':'filesystem','source':'sandbox-0'},
        spec={'agent':{'token':'DO-NOT-EXPOSE-SNAPSHOT-TOKEN'}},locations=[{'token':'DO-NOT-EXPOSE-REPLICA-TOKEN'}]))


@contextmanager
def dashboard_server(controller):
    dashboard=Dashboard(controller,'test-controller-token',interval=1)
    dashboard.monitor.refresh()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_GET(self):
            if not dashboard.handle(self):
                self.send_error(404)
        do_POST=do_GET
        do_HEAD=do_GET
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=True
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield dashboard,'http://127.0.0.1:'+str(server.server_port)
    finally:
        server.shutdown();server.server_close();thread.join()
        dashboard.monitor.close()


def request(url,path,*,method='GET',body=None,cookie='',headers=None):
    parsed=urlsplit(url)
    connection=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=10)
    try:
        connection.request(method,path,body=json.dumps(body) if body is not None else None,
            headers={'Content-Type':'application/json',**({'Cookie':cookie} if cookie else {}),**(headers or {})})
        response=connection.getresponse()
        return response.status,dict(response.getheaders()),response.read()
    finally:
        connection.close()


def test_browser_sessions_are_read_only_scoped_and_revocable(tmp_path, monkeypatch):
    monkeypatch.setenv('SANDWEAVE_HOME',str(tmp_path/'client'))
    cluster=Cluster.start('dashboard-test',directory=tmp_path/'controller',local_worker=False,monitor_interval=1)
    url=cluster.info['connection']['address']
    try:
        assert request(url,'/dashboard/')[0]==200
        assert request(url,'/dashboard/api/overview')[0]==401
        assert request(url,'/metrics')[0]==401
        link=cluster.dashboard()
        ticket=link.split('#ticket=')[1]
        code,headers,_=request(url,'/dashboard/api/session',method='POST',body={'ticket':ticket})
        assert code==200
        assert 'HttpOnly' in headers['Set-Cookie'] and 'SameSite=Strict' in headers['Set-Cookie']
        cookie=headers['Set-Cookie'].split(';')[0]
        assert cluster.connection.token not in cookie
        assert request(url,'/dashboard/api/session',method='POST',body={'ticket':ticket})[0]==401
        assert request(url,'/dashboard/api/overview',cookie=cookie)[0]==200
        # A monitor cookie never authorizes the administrative RPC endpoint.
        connection=http.client.HTTPConnection('127.0.0.1',urlsplit(url).port)
        connection.request('POST','/rpc',body=encode({'op':'shutdown','params':{}}),headers={'Cookie':cookie})
        response=connection.getresponse();assert response.status==403;response.read();connection.close()
        assert cluster.connection.call('ping')['cluster_id']
        assert request(url,'/dashboard/api/logs',method='POST',cookie=cookie)[0]==405
        assert request(url,'/dashboard/api/overview',cookie=cookie,headers={'Origin':'https://evil.example'})[0]==403
        assert request(url,'/dashboard/api/overview',cookie=cookie,headers={'Sec-Fetch-Site':'cross-site'})[0]==403
        assert request(url,'/dashboard/api/logout',method='POST',cookie=cookie)[0]==200
        assert request(url,'/dashboard/api/overview',cookie=cookie)[0]==401
        code,_,text=request(url,'/metrics',headers={'Authorization':'Bearer '+cluster.connection.token})
        assert code==200 and b'sandweave_monitor_sample_timestamp_seconds' in text
    finally:
        cluster.stop();cluster.close()


def test_projection_filters_counts_history_and_secrets(controller):
    seed(controller,count=120)
    monitor=Monitor(controller,max_samples=100,interval=1)
    try:
        monitor.refresh()
        view=monitor.overview()
        assert view['summary']['sandboxes']==120
        assert view['summary']['measured_sandboxes']==120
        assert view['summary']['reserved']['slots']==123
        result=monitor.listing('sandboxes',pool='pool-0',worker='worker-0',limit=7)
        assert result['total']==20 and len(result['items'])==7
        assert monitor.listing('workers',q='training')['total']==3
        serialized=json.dumps(monitor.view)
        assert 'DO-NOT-EXPOSE' not in serialized
        assert monitor.detail('snapshots','snap-example')['kind']=='filesystem'
        assert monitor.listing('tasks',pool='job-example')['total']==1
        assert monitor.logs('tasks','task-example')['text']=='completed\n'
        assert monitor.logs('tasks','task-example',stream='stderr',attempt='task-example-0')['text']=='retry requested\n'
        with pytest.raises(ValueError):
            monitor.listing('sandboxes',limit=10000)
        assert len(monitor.history()['points'])<=181
        assert monitor.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]<=100
        event=monitor.events(limit=1)
        assert event['more'] and len(event['items'])==1
        assert monitor.events(before=event['items'][0]['sequence'],limit=1)['items'][0]['sequence']<event['items'][0]['sequence']
    finally:
        monitor.close()


def test_missing_measurements_remain_unavailable_in_aggregates(controller):
    seed(controller)
    monitor = Monitor(controller)
    try:
        for worker in controller.state.list('worker'):
            for metric in worker['inventory']['telemetry']['sandboxes'].values():
                metric['cpu_cores'] = None
            controller.state.put('worker', worker)
        monitor.refresh()
        assert monitor.overview()['summary']['cpu_cores'] is None
        assert monitor.overview()['summary']['rss_bytes'] > 0
        assert all(p['cpu_cores'] is None for p in monitor.listing('pools')['items'])
        for worker in controller.state.list('worker'):
            for metric in worker['inventory']['telemetry']['sandboxes'].values():
                metric.update(cpu_cores=0, rss_bytes=0)
            controller.state.put('worker', worker)
        monitor.refresh()
        assert monitor.overview()['summary']['cpu_cores'] == 0
        assert all(p['rss_bytes'] == 0 for p in monitor.listing('pools')['items'])
        for worker in controller.state.list('worker'):
            worker['inventory']['telemetry']['time'] = time.time() - 60
            controller.state.put('worker', worker)
        monitor.refresh()
        assert monitor.overview()['summary']['cpu_cores'] is None
        assert all(p['rss_bytes'] is None for p in monitor.listing('pools')['items'])
        assert all(w['telemetry']['sandbox_rss_bytes'] is None for w in monitor.listing('workers')['items'])
    finally:
        monitor.close()


def test_stale_workers_do_not_report_zero_usage_and_retention_survives_restart(controller):
    seed(controller)
    monitor=Monitor(controller,history_hours=1)
    monitor.refresh()
    worker=controller.state.get('worker','worker-0')
    worker['inventory']['telemetry']['time']=time.time()-60
    controller.state.put('worker',worker)
    monitor.refresh()
    assert monitor.overview()['summary']['stale_workers']==1
    assert monitor.detail('sandboxes','sandbox-0')['telemetry']=={}
    assert 'worker="worker-0"} 23' not in monitor.prometheus()
    monitor.db.execute('INSERT INTO samples(time,kind,id,data) VALUES (?,?,?,?)',(time.time()-7200,'cluster','cluster','{}'))
    monitor.refresh()
    assert monitor.db.execute('SELECT MIN(time) FROM samples').fetchone()[0]>time.time()-3600
    monitor.close()
    reopened=Monitor(controller)
    try:
        assert reopened.history()['points']
    finally:
        reopened.close()


def test_monitoring_metadata_omits_binary_logs(controller):
    controller.state.put('task',dict(id='large',parent='job',state='succeeded',
        result={'stdout':b'x'*8*1024**2,'stderr':b'error','returncode':7}))
    metadata=controller.state.metadata('task')
    assert metadata[0]['result']=={'stdout':None,'stderr':None,'returncode':7}
    assert len(json.dumps(metadata))<1024


def test_login_validation_expiry_limits_and_static_headers(controller):
    with dashboard_server(controller) as (dashboard,url):
        code,headers,body=request(url,'/dashboard/')
        assert code==200 and b'app.js' in body
        assert headers['Content-Security-Policy'].startswith("default-src 'none'")
        assert headers['Cache-Control']=='no-store' and headers['X-Frame-Options']=='DENY'
        expired=dashboard.ticket()['ticket']
        dashboard.tickets[dashboard._hash(expired)]=time.time()-1
        assert request(url,'/dashboard/api/session',method='POST',body={'ticket':expired})[0]==401
        for _ in range(19):
            assert request(url,'/dashboard/api/session',method='POST',body={'token':'wrong'})[0]==401
        assert request(url,'/dashboard/api/session',method='POST',body={'token':'wrong'})[0]==429
        assert request(url,'/dashboard/../credentials.json')[0] in (401,404)


def test_pid_reuse_descendants_and_bounded_log_tail(tmp_path):
    table={10:dict(ppid=1,start=99),11:dict(ppid=10,start=100),12:dict(ppid=11,start=101)}
    assert members(table,[{'pid':10,'start':98}])==set()
    assert members(table,[{'pid':10,'start':99}])=={10,11,12}
    path=tmp_path/'runtime.log';path.write_bytes(b'a'*100000+b'last line\n')
    assert tail(path,100)['text'].endswith('last line\n') and tail(path,100)['truncated']
    with pytest.raises(ValueError):
        tail(path,10000000)
    sampler=Sampler(tmp_path)
    sampled=sampler.sample([],[])
    assert sampled['disk_total_bytes']>0 and sampled['host_memory_total_bytes']>0
    assert sampled['gpus']==[] and sampled['cpu_busy_percent'] is None
    assert os.getpid() in process_table()


def test_monitoring_errors_never_fail_worker_inventory(tmp_path, monkeypatch):
    sampler=Sampler(tmp_path)
    monkeypatch.setattr(sampler,'_sample',lambda *args: (_ for _ in ()).throw(PermissionError('restricted proc')))
    value=sampler.sample([],[])
    assert 'PermissionError' in value['error']


def test_gpu_unavailable_counters_are_not_zero(tmp_path, monkeypatch):
    from sandweave.sandbox.telemetry import gpu_metrics
    monkeypatch.setattr('subprocess.check_output', lambda *a, **k: 'GPU-test, 38, 512, 48000, [N/A], 210.5\n')
    values,error=gpu_metrics([{'uuid':'GPU-test'}])
    assert error is None and values[0]['utilization_percent']==38
    assert values[0]['memory_used_bytes']==512*1024**2
    assert values[0]['temperature_c'] is None


def test_old_template_workspace_keeps_its_measurements(controller):
    seed(controller)
    worker=controller.state.get('worker','worker-0')
    old=worker['inventory']['telemetry']
    worker['instances']={'old':{'telemetry':old}}
    worker['inventory']['telemetry']={**old,'sandboxes':{}}
    controller.state.put('worker',worker)
    monitor=Monitor(controller)
    try:
        monitor.refresh()
        assert monitor.detail('sandboxes','sandbox-0')['telemetry']['rss_bytes']>0
    finally:
        monitor.close()


def test_https_cookie_and_browser_session_survive_no_admin_rpc(tmp_path, monkeypatch):
    import ssl
    import subprocess
    monkeypatch.setenv('SANDWEAVE_HOME',str(tmp_path/'client'))
    cert,key=tmp_path/'cert.pem',tmp_path/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
        '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1',
        '-keyout',str(key),'-out',str(cert)],check=True,capture_output=True)
    cluster=Cluster.start('secure-dashboard',directory=tmp_path/'controller',local_worker=False,
        listen='127.0.0.1:0',tls_cert=cert,tls_key=key)
    parsed=urlsplit(cluster.info['connection']['address'])
    connection=http.client.HTTPSConnection(parsed.hostname,parsed.port,context=ssl.create_default_context(cafile=cert))
    try:
        ticket=cluster.dashboard().split('#ticket=')[1]
        connection.request('POST','/dashboard/api/session',json.dumps({'ticket':ticket}),{'Content-Type':'application/json'})
        response=connection.getresponse();response.read()
        assert response.status==200
        assert '; Secure' in response.getheader('Set-Cookie')
    finally:
        connection.close();cluster.stop();cluster.close()


def test_busy_monitor_limits_do_not_block_static_page_or_health(controller):
    with dashboard_server(controller) as (dashboard,url):
        _,headers,_=request(url,'/dashboard/api/session',method='POST',body={'token':'test-controller-token'})
        cookie=headers['Set-Cookie'].split(';')[0]
        for _ in range(8):
            assert dashboard.requests.acquire(False)
        try:
            assert request(url,'/dashboard/api/resources?kind=workers',cookie=cookie)[0]==503
            assert request(url,'/dashboard/')[0]==200
        finally:
            for _ in range(8):dashboard.requests.release()
        assert request(url,'/dashboard/api/overview',cookie=cookie)[0]==200


def test_large_inventory_pages_are_bounded_and_responsive(controller,tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    seed(controller,count=1000)
    with dashboard_server(controller) as (dashboard,url):
        _,headers,_=request(url,'/dashboard/api/session',method='POST',body={'token':'test-controller-token'})
        cookie=headers['Set-Cookie'].split(';')[0]
        def read_page(i):
            started=time.monotonic()
            code,_,body=request(url,'/dashboard/api/resources?kind=sandboxes&limit=50&offset='+str(i%20*50),cookie=cookie)
            assert code==200
            data=json.loads(body)
            assert data['total']==1000 and len(data['items'])==50
            assert len(body)<200000
            return time.monotonic()-started
        with ThreadPoolExecutor(max_workers=4) as clients:
            elapsed=sorted(clients.map(read_page,range(40)))
        receipt={'resources':1000,'clients':4,'requests':40,'p50_seconds':elapsed[19],'p95_seconds':elapsed[37],'max_seconds':elapsed[-1]}
        (tmp_path/'dashboard-load.json').write_text(json.dumps(receipt))
        print('DASHBOARD_LOAD',json.dumps(receipt))
        assert elapsed[-1]<5
