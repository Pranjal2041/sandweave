"""Real process/thread signal state and JDK attach inside the released engine."""
import json
import os
from pathlib import Path
import textwrap
import time

import pytest
from sandweave import Sandbox, CPU, Memory, Mount
from sandweave.sandbox.targets import local_connection

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.environ.get('SANDWEAVE_INTEGRATION'), reason='explicit disposable worker required')]


@pytest.fixture(scope='module', autouse=True)
def stop_owned_worker():
    yield
    c = local_connection()
    try:
        c.call('_shutdown_if_idle')
    finally:
        c.close()


def test_signal_masks_reflect_pending_ignored_caught_and_thread_blocking():
    program = '''
        import os, signal, threading
        def masks(path='/proc/self/status'):
            fields = dict(line.split(':',1) for line in open(path) if ':' in line)
            return {k:int(fields[k].strip(),16) for k in ('SigPnd','ShdPnd','SigBlk','SigIgn','SigCgt')}
        bit = lambda sig: 1 << (sig-1)
        signal.signal(signal.SIGTERM,lambda *a:None)
        signal.signal(signal.SIGUSR1,lambda *a:None)
        signal.signal(signal.SIGHUP,lambda *a:None)
        signal.signal(signal.SIGUSR2,signal.SIG_IGN)
        signal.pthread_sigmask(signal.SIG_BLOCK,[signal.SIGUSR1,signal.SIGHUP])
        signal.raise_signal(signal.SIGUSR1)
        os.kill(os.getpid(),signal.SIGHUP)
        m=masks(); pair=bit(signal.SIGUSR1)|bit(signal.SIGHUP)
        assert m['SigPnd'] & pair == bit(signal.SIGUSR1),m
        assert m['ShdPnd'] & pair == bit(signal.SIGHUP),m
        assert m['SigBlk'] & pair == pair,m
        assert m['SigIgn'] & bit(signal.SIGUSR2),m
        assert m['SigCgt'] & (pair|bit(signal.SIGTERM)) == pair|bit(signal.SIGTERM),m
        assert signal.sigtimedwait([signal.SIGUSR1],0)
        assert signal.sigtimedwait([signal.SIGHUP],0)
        results=[]
        def thread():
            signal.pthread_sigmask(signal.SIG_BLOCK,[signal.SIGTERM])
            results.append(masks('/proc/self/task/%s/status'%threading.get_native_id()))
        t=threading.Thread(target=thread);t.start();t.join()
        assert results[0]['SigBlk'] & bit(signal.SIGTERM)
        assert not masks()['SigBlk'] & bit(signal.SIGTERM)
        print('signal masks passed')
    '''
    with Sandbox() as env:
        result=env.run(argv=['python3','-c',textwrap.dedent(program)],check=True)
        assert result.stdout == 'signal masks passed\n'


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_PROC_TEST_BINARY'), reason='requires built upstream proc_test')
def test_upstream_proc_signal_mask_regression():
    binary = Path(os.environ['SANDWEAVE_PROC_TEST_BINARY']).resolve()
    with Sandbox(mounts=[Mount(str(binary), '/input/proc_test')]) as env:
        result = env.run(argv=['/input/proc_test','--gtest_filter=ProcPidStatusTest.SignalMasks'],
                         timeout=30,check=True)
        assert '[  PASSED  ] 1 test.' in result.stdout


@pytest.mark.skipif(not os.environ.get('SANDWEAVE_ELASTICSEARCH_ARCHIVE'), reason='requires verified Elasticsearch 9.2.0 archive')
def test_jdk25_attach_and_elasticsearch9_startup(tmp_path):
    archive=Path(os.environ['SANDWEAVE_ELASTICSEARCH_ARCHIVE']).resolve()
    with Sandbox(cpu=CPU(vcpus=2),memory=Memory('4GiB','1GiB'),
                 mounts=[Mount(str(archive),'/input/elasticsearch.tar.gz',read_only=True)]) as env:
        env.run('mkdir -p /opt/elasticsearch; tar -xzf /input/elasticsearch.tar.gz -C /opt/elasticsearch --strip-components=1; '
                'useradd -m es-test; chown -R es-test:es-test /opt/elasticsearch',timeout=180,check=True)
        java='/opt/elasticsearch/jdk/bin/java'
        version=env.run(java+' -version',check=True).stderr
        assert 'version "25' in version,version
        env.files.write_text('/workspace/AttachTarget.java',
            'class AttachTarget { public static void main(String[] a) throws Exception { '
            'System.out.println(ProcessHandle.current().pid()); Thread.sleep(120000); }}')
        process=env.exec(java+' /workspace/AttachTarget.java',user='es-test')
        try:
            pid=int(process.stdout.readline())
            result=env.run(f'/opt/elasticsearch/jdk/bin/jcmd {pid} VM.version',user='es-test',timeout=30,check=True)
            assert '25' in result.stdout
            (tmp_path/'jdk-attach.txt').write_text(version+'\n'+result.stdout)
        finally:
            process.terminate()
        env.files.write_text('/opt/elasticsearch/config/elasticsearch.yml',
            'cluster.name: sandweave-acceptance\nnode.name: acceptance\ndiscovery.type: single-node\n'
            'network.host: 127.0.0.1\nxpack.security.enabled: false\n')
        process=env.exec('/opt/elasticsearch/bin/elasticsearch > /opt/elasticsearch/startup.log 2>&1',
                         user='es-test',env={'ES_JAVA_OPTS':'-Xms512m -Xmx512m'})
        try:
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                result=env.run('curl -fsS http://127.0.0.1:9200',timeout=5)
                if result.returncode==0:
                    info=json.loads(result.stdout)
                    assert info['version']['number']=='9.2.0'
                    health=env.run("curl -fsS 'http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow&timeout=30s'",timeout=40,check=True)
                    assert json.loads(health.stdout)['status'] in ('green','yellow')
                    (tmp_path/'elasticsearch.json').write_text(result.stdout+'\n'+health.stdout)
                    break
                if process.poll() is not None:
                    pytest.fail('Elasticsearch exited: '+env.files.read_text('/opt/elasticsearch/startup.log')[-8000:])
                time.sleep(.5)
            else:
                pytest.fail('Elasticsearch startup timed out')
        finally:
            (tmp_path/'elasticsearch-startup.log').write_text(env.files.read_text('/opt/elasticsearch/startup.log'))
            process.terminate()
