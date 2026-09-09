import copy
import json
from types import SimpleNamespace

import pytest

from sandweave import Sandbox
from sandweave.sandbox.information import summarize
from sandweave.sandbox.resources import CPU, Memory, normalize
from sandweave.templates.resolve import Template


def record(template='gnome', *, gpu=False, state='ready', runtime_state='running'):
    return {'id': 'sw-info-test', 'name': 'workbench', 'state': state,
            'spec': {'template': Template(template).resolve(), 'runtime': 'gvisor',
                     'resources': normalize(cpu=CPU(4, weight=200, quota=1.5),
                                            memory=Memory('8GiB', '1GiB'), gpu=gpu),
                     'env': {'SECRET': 'private-environment'}},
            'worker': {'hostname': 'worker.example', 'job_id': '12345'},
            'agent': {'token': 'private-agent'}, 'owner': 'private-owner',
            'runtime_status': {'status': runtime_state, 'ports': {'5901': 43210, '23799': 55555}}}


def test_summary_is_json_serializable_and_does_not_return_recipe_payloads_or_secrets():
    source = record()
    source['spec']['template']['setup_steps'] = [{'files': {'setup.sh': b'private script'}}]
    value = summarize(source)
    serialized = json.dumps(value)
    assert 'private' not in serialized and '55555' not in serialized
    assert value['cpu'] == {'vcpus': 4, 'weight': 200, 'quota': 1.5}
    assert value['memory'] == {'guest': '8GiB', 'runtime': '1GiB'}
    value['cpu']['weight'] = 1
    assert source['spec']['resources']['cpu']['weight'] == 200


def test_summary_reports_vnc_endpoint_without_scheduler_metadata_or_connection_commands():
    value = summarize(record())
    assert value['worker'] == {'hostname': 'worker.example'}
    assert value['vnc'] == {'worker_host': 'worker.example', 'port': 43210,
                            'url': 'vnc://127.0.0.1:43210'}
    serialized = json.dumps(value)
    assert 'job_id' not in serialized and 'ssh_command' not in serialized


@pytest.mark.parametrize('template,state,runtime_state', [
    ('coding', 'ready', 'running'), ('gnome', 'paused', 'paused'),
    ('gnome', 'terminated', 'stopped'), ('gnome', 'failed', 'stopped'),
    ('gnome', 'preparing', 'running'), ('gnome', 'ready', 'missing')])
def test_reserved_or_stale_port_is_not_advertised_as_a_ready_desktop(template, state, runtime_state):
    assert summarize(record(template, state=state, runtime_state=runtime_state))['vnc'] is None


def test_missing_port_and_other_desktop_backends_have_no_vnc_url():
    source = record()
    source['runtime_status']['ports'] = {}
    assert summarize(source)['vnc'] is None
    source = record()
    source['spec']['template']['capabilities']['desktop']['backend'] = 'wayland'
    assert summarize(source)['vnc'] is None


def test_legacy_worker_uses_launcher_host_and_does_not_invent_gpu_selection():
    source = record(gpu='L40S')
    del source['worker']
    source['runtime_status']['launcher'] = {'hostname': 'old-worker'}
    info = summarize(source)
    assert info['worker'] == {'hostname': 'old-worker'}
    assert info['gpus'] is None
    assert info['vnc']['worker_host'] == 'old-worker'
    del source['runtime_status']['launcher']
    info = summarize(source)
    assert info['worker']['hostname'] is None and info['vnc']['worker_host'] is None
    source['runtime_status']['status'] = 'stopped'
    assert summarize(source)['gpus'] == []


def test_info_fetches_current_state_and_mutation_cannot_change_the_handle():
    source = record()
    env = Sandbox.__new__(Sandbox)
    env.id, env._closed = source['id'], False
    env._connection = SimpleNamespace(call=lambda *a, **kw: copy.deepcopy(source))
    assert env.info['state'] == 'ready'
    env.info['cpu']['vcpus'] = 100
    assert env.spec['resources']['cpu']['vcpus'] == 4
    source['state'] = 'terminated'
    source['runtime_status']['status'] = 'stopped'
    assert env.info['state'] == 'terminated' and env.info['vnc'] is None
    env._closed = True
    with pytest.raises(RuntimeError, match='closed'):
        _ = env.info


def test_gpu_model_matches_recorded_uuid_not_a_reused_device_minor(tmp_path):
    from sandweave.sandbox.runtimes.gpu_information import describe
    directory = tmp_path / '0000:01:00.0'
    directory.mkdir()
    info = directory / 'information'
    info.write_text('Model: NVIDIA L40S\nGPU UUID: GPU-new\nDevice Minor: 0\n')
    device = {'device_minor': 0, 'uuid': 'GPU-old'}
    assert describe(device, directory=tmp_path) == {'device': '/dev/nvidia0', 'uuid': 'GPU-old', 'model': None}
    device['uuid'] = 'GPU-new'
    assert describe(device, directory=tmp_path)['model'] == 'NVIDIA L40S'
    assert describe(device, directory=tmp_path / 'missing')['model'] is None


def test_runtime_gpu_report_uses_launch_identity_and_survives_missing_metadata(monkeypatch):
    from sandweave.sandbox.runtimes.gvisor.driver import Runtime
    from sandweave.sandbox.runtimes import gpu_information
    selected = {'device_minor': 3, 'uuid': 'GPU-selected'}
    runtime = Runtime.__new__(Runtime)
    runtime.manager = SimpleNamespace(status=lambda _: {'status': 'paused', 'gpu': True},
                                      _settings=lambda _: {'gpu': selected})
    monkeypatch.setattr(gpu_information, 'describe', lambda device: dict(device))
    assert runtime.status('sw-info')['gpus'] == [selected]
    runtime.manager._settings = lambda _: {}
    assert runtime.status('sw-info')['gpus'] is None
    runtime.manager.status = lambda _: {'status': 'stopped', 'gpu': True}
    assert runtime.status('sw-info')['gpus'] == []


def test_ssh_target_still_connects_through_the_control_tunnel(monkeypatch):
    from sandweave.sandbox import targets
    monkeypatch.setattr(targets, '_ssh', lambda *a, **kw: json.dumps({'port': 1234, 'token': 'secret'}))
    monkeypatch.setattr(targets, '_tunnel', lambda host, port: 54321)
    monkeypatch.setattr(targets.Connection, 'call', lambda *a, **kw: {})
    connection = targets.connect({'host': 'alice@compute-alias', 'metadata': '/worker.json'})
    try:
        assert connection.port == 54321
    finally:
        connection.close()


def test_slurm_target_uses_a_control_tunnel_only_for_remote_placement(monkeypatch, tmp_path):
    from sandweave.sandbox import targets
    monkeypatch.setattr(targets, 'home', lambda: tmp_path)
    monkeypatch.setattr(targets.Slurm, '_wait', lambda *a, **kw: {'NumNodes': '1', 'NodeList': 'worker'})
    monkeypatch.setattr(targets.subprocess, 'check_output', lambda *a, **kw: 'worker\n')
    monkeypatch.setattr(targets, '_tunnel', lambda host, port: 54321)
    monkeypatch.setattr(targets.Connection, 'call', lambda *a, **kw: {})
    metadata = tmp_path / 'worker.json'
    metadata.write_text(json.dumps({'port': 1234, 'token': 'secret'}))
    target = targets.Slurm.connect('12345', host='alice@compute-alias', metadata=str(metadata))
    for client_host, expected in [('client', 54321), ('worker', 1234)]:
        monkeypatch.setattr(targets.socket, 'gethostname', lambda: client_host)
        connection = target.connection()
        try:
            assert connection.port == expected
        finally:
            connection.close()
