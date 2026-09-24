"""Guest-only restore overrides retain the saved runtime memory allowance."""
import copy

import pytest

from sandweave import Memory
from sandweave.sandbox.sandbox import definition


@pytest.mark.parametrize('source', ['snapshot', 'cache'])
@pytest.mark.parametrize('memory, expected', [
    (None, {'guest': '2GiB', 'runtime': '768MiB'}),
    ('3GiB', {'guest': '3GiB', 'runtime': '768MiB'}),
    (3 * 1024**3, {'guest': 3 * 1024**3, 'runtime': '768MiB'}),
    (Memory('3GiB', '1GiB'), {'guest': '3GiB', 'runtime': '1GiB'}),
    (Memory('3GiB'), {'guest': '3GiB', 'runtime': '512MiB'}),
    ({'guest': '3GiB', 'runtime': '256MiB'}, {'guest': '3GiB', 'runtime': '256MiB'}),
])
def test_restore_memory_override_precedence(monkeypatch, source, memory, expected):
    saved = definition(memory=Memory('2GiB', '768MiB'))['spec']
    original = copy.deepcopy(saved)

    class Connection:
        def call(self, operation, **kwargs):
            assert operation == 'snapshot_spec'
            assert kwargs == {'reference': 'saved'}
            return {'reference': 'snap-pinned', 'spec': saved}

        def close(self):
            pass

    monkeypatch.setattr('sandweave.sandbox.sandbox.connect', lambda target: Connection())
    restored = definition(**{source: 'saved'}, memory=memory)
    assert restored['reference'] == 'snap-pinned'
    assert restored['spec']['resources']['memory'] == expected
    assert saved == original


@pytest.mark.parametrize('template, runtime', [('coding', '512MiB'), ('gnome', '1GiB')])
def test_fresh_guest_memory_override_keeps_template_runtime(template, runtime):
    assert definition(template=template, memory='3GiB')['spec']['resources']['memory'] == {
        'guest': '3GiB', 'runtime': runtime}
