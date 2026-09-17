import pytest

from sandweave.sandbox.runtimes.gvisor.driver import Runtime
from sandweave.sandbox.sandbox import definition


@pytest.mark.parametrize('options, expected', [
    ({}, False),
    ({'docker_data': False}, False),
    ({'docker_data': True}, True),
    ({'docker_archive': 'images/old.tar'}, True),
    ({'docker_data': True, 'docker_archive': 'images/old.tar'}, True),
])
def test_storage_selection_independent_of_archive(tmp_path, options, expected):
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    spec = definition(template={'extends': 'coding', 'runtime_options': options})['spec']
    assert ('--docker-data' in runtime.options(spec)) is expected


def test_builtin_docker_defaults_to_empty_persistent_storage(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    spec = definition(template='docker')['spec']
    assert '--docker-data' in runtime.options(spec)
    assert not spec['template']['runtime_options'].get('docker_archive')
    disabled = definition(template={'extends': 'docker', 'runtime_options': {'docker_data': False}})['spec']
    assert '--docker-data' not in runtime.options(disabled)


@pytest.mark.parametrize('options', [
    {'docker_data': 'false'}, {'docker_data': 1},
    {'docker_data': False, 'docker_archive': 'images/old.tar'},
])
def test_invalid_storage_setting_is_not_silently_ignored(tmp_path, options):
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    spec = definition(template={'extends': 'coding', 'runtime_options': options})['spec']
    with pytest.raises(ValueError, match='docker_'):
        runtime.options(spec)
