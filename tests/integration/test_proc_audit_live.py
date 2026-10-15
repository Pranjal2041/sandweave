"""Process visibility and protected audit acceptance using the public SDK."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import statistics
import time

import pytest

from sandweave import Sandbox, Memory, SandboxError
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit isolated worker required')]
MEMORY = Memory('512MiB', '512MiB')


@pytest.fixture(scope='module', autouse=True)
def stop_idle_worker():
    yield
    connection = local_connection()
    deadline = time.monotonic() + 15
    try:
        while True:
            try:
                connection.call('_shutdown_if_idle')
                break
            except SandboxError as error:
                if str(error) != 'worker still owns live sandboxes' or time.monotonic() > deadline:
                    raise
                time.sleep(.1)
    finally:
        connection.close()


@pytest.mark.parametrize('mode', [0, 1, 2])
def test_proc_visibility_and_cached_direct_paths(mode):
    env = Sandbox(hidepid=mode, memory=MEMORY)
    try:
        root_pid = int(env.run(argv=['python3', '-c',
            'import subprocess; p=subprocess.Popen(["sleep","120"],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print(p.pid)']).stdout)
        # Warm both directory and file dentries with a privileged caller.
        assert 'sleep' in env.run('cat /proc/%d/cmdline' % root_pid).stdout
        code = """import os,json,sys,subprocess
pid=sys.argv[1]
r={'listed':pid in os.listdir('/proc')}
for name,path in [('stat','/proc/'+pid),('cmdline','/proc/'+pid+'/cmdline'),('task','/proc/'+pid+'/task/'+pid+'/status')]:
 try:
  os.stat(path) if name=='stat' else open(path,'rb').read()
  r[name]=0
 except OSError as e:r[name]=e.errno
p=subprocess.Popen(['sleep','30'])
try:
 r['same_uid']=str(p.pid) in os.listdir('/proc') and bool(open('/proc/'+str(p.pid)+'/cmdline','rb').read())
 r['self']=bool(open('/proc/self/status').read())
finally:p.terminate();p.wait()
print(json.dumps(r))
"""
        result = json.loads(env.run(argv=['python3', '-c', code, str(root_pid)], user='1001:1001').stdout)
        assert result['same_uid'] and result['self']
        assert result['listed'] == (mode != 2)
        assert result['stat'] == (2 if mode == 2 else 0)
        assert result['cmdline'] == ({0: 0, 1: 13, 2: 2}[mode])
        assert result['task'] == ({0: 0, 1: 13, 2: 2}[mode])
        assert 'sleep' in env.run('cat /proc/%d/cmdline' % root_pid).stdout
        mounts = env.run('cat /proc/mounts').stdout
        assert ('hidepid=%d' % mode) in mounts if mode else 'hidepid=' not in mounts
        if mode == 2:
            saved = env.snapshot(state='memory')
            restored = Sandbox(snapshot=saved, memory=MEMORY)
            try:
                assert 'hidepid=2' in restored.run('cat /proc/mounts').stdout
                result = json.loads(restored.run(argv=['python3', '-c', code, str(root_pid)], user='1001:1001').stdout)
                assert not result['listed'] and result['cmdline'] == 2
            finally:
                restored.terminate()
                restored.close()
    finally:
        env.terminate()
        env.close()


def test_audit_concurrency_restore_and_failure(tmp_path):
    audited = Sandbox(audit=True, hidepid=2, memory=MEMORY)
    plain = Sandbox(memory=MEMORY)
    try:
        # Guest root cannot discover the host log through the guest filesystem.
        assert plain.run('test ! -e /lab/runs').returncode == 0
        assert audited.run('test ! -e /lab/runs').returncode == 0
        def action(index):
            start = time.monotonic()
            selected = plain if index % 4 == 0 else audited
            result = selected.run('printf audit-marker-%d > /tmp/audit-marker-%d; cat /tmp/audit-marker-%d' % (index,index,index))
            assert result.stdout == 'audit-marker-%d' % index
            return time.monotonic()-start
        with ThreadPoolExecutor(max_workers=16) as executor:
            durations = list(executor.map(action, range(48)))
        status = audited.audit_log(tmp_path/'audit.jsonl')
        assert status['complete'], status
        assert status['dropped'] == 0
        lines = (tmp_path/'audit.jsonl').read_text().splitlines()
        records = [json.loads(line) for line in lines]
        assert len(records) == status['events'] == status['last_sequence']
        assert [r['sequence'] for r in records] == list(range(1,len(records)+1))
        assert any(r['type']=='MESSAGE_SYSCALL_OPEN' and 'audit-marker-' in r['data'].get('pathname','') for r in records)
        assert not plain.spec['template']['runtime_options'].get('audit')
        with pytest.raises(Exception, match='filesystem snapshots'):
            audited.snapshot(state='memory')
        saved = audited.snapshot(state='filesystem')
        restored = Sandbox(snapshot=saved, memory=MEMORY)
        try:
            assert restored.spec['template']['runtime_options']['audit']
            assert 'hidepid=2' in restored.run('cat /proc/mounts').stdout
            assert restored.audit_log(tmp_path/'restored.jsonl')['complete']
        finally:
            restored.terminate()
            restored.close()
        print(json.dumps({'concurrent_commands':len(durations),'median_seconds':statistics.median(durations),
                          'max_seconds':max(durations),'events':status['events'],'dropped':status['dropped']}))
    finally:
        audited.terminate()
        audited.close()
        plain.terminate()
        plain.close()
    limited = Sandbox(template={'extends':'coding','runtime_options':{'audit_max_bytes':1024}},audit=True,memory=MEMORY)
    try:
        assert limited.run('printf still-running').stdout == 'still-running'
        status = limited.audit_log(tmp_path/'limited.jsonl')
        assert not status['complete'] and status['error']
    finally:
        limited.terminate()
        limited.close()
