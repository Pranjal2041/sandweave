import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sandweave.cli import creation, parser
from sandweave.sandbox.clock import check_worker, configure, features, runtime_options, summary
from sandweave.sandbox.errors import UnsupportedFeature
from sandweave.sandbox.information import summarize
from sandweave.sandbox.runtimes.gvisor.driver import Runtime
from sandweave.sandbox.sandbox import definition


@pytest.mark.parametrize('value, expected', [
    (-3600, -3600 * 10**9),
    (1.5, 1_500_000_000),
    (timedelta(days=-400, microseconds=7), (-400 * 86400) * 10**9 + 7000),
])
def test_offsets_are_exact_nanoseconds_and_freeze_by_default(value, expected):
    assert configure(clock_offset=value) == {'offset_ns': expected, 'pause': 'freeze'}


@pytest.mark.parametrize('value', [datetime(2025, 3, 1, 1, tzinfo=timezone(timedelta(hours=1))),
                                   '2025-03-01T00:00:00Z', '2025-03-01T00:00:00+00:00'])
def test_start_accepts_aware_datetimes_and_iso_strings(value):
    clock = configure(clock_start=value)
    assert clock == {'start_ns': 1740787200 * 10**9, 'pause': 'freeze'}
    assert summary(clock) == {'start': '2025-03-01T00:00:00+00:00', 'pause': 'freeze'}


def test_pause_default_and_overrides():
    assert configure() is None
    assert configure(clock_offset=60, clock_pause='continue') == {'offset_ns': 60 * 10**9, 'pause': 'continue'}
    # Pause behavior alone keeps host time.
    assert configure(clock_pause='freeze') == {'pause': 'freeze'}
    assert configure(clock_pause='continue') == {'pause': 'continue'}


@pytest.mark.parametrize('options', [
    {'clock_offset': 1, 'clock_start': '2025-03-01T00:00:00Z'},
    {'clock_offset': True}, {'clock_offset': '3600'}, {'clock_offset': float('nan')},
    {'clock_offset': 2**63}, {'clock_offset': -10**12},
    {'clock_start': datetime(2025, 3, 1)}, {'clock_start': '2025-03-01'}, {'clock_start': 'tomorrow'},
    {'clock_start': 1740787200}, {'clock_start': '1960-01-01T00:00:00Z'}, {'clock_start': '2200-01-01T00:00:00Z'},
    {'clock_pause': 'stop'}, {'clock_pause': True},
])
def test_invalid_values(options):
    with pytest.raises(ValueError):
        definition(**options)


def test_requires_gvisor_and_updated_workers():
    with pytest.raises(UnsupportedFeature):
        definition(runtime='apptainer', clock_offset=60)
    spec = definition(clock_offset=60)['spec']
    with pytest.raises(UnsupportedFeature, match='0.2.45'):
        check_worker(spec, SimpleNamespace(call=lambda op: {}))
    check_worker(spec, SimpleNamespace(call=lambda op: {'guest_clock': 1}))
    check_worker(definition()['spec'], SimpleNamespace(call=lambda op: {}))


def test_launcher_options_and_engine_features(tmp_path):
    runtime = Runtime.__new__(Runtime)
    runtime.root = tmp_path
    assert '--clock-pause' not in runtime.options(definition()['spec'])
    options = runtime.options(definition(clock_offset=-90)['spec'])
    assert options[options.index('--clock-offset-ns') + 1] == str(-90 * 10**9)
    assert options[options.index('--clock-pause') + 1] == 'freeze'
    assert runtime_options({'start_ns': 5, 'pause': 'continue'}) == ['--clock-start-ns', '5', '--clock-pause', 'continue']
    assert features({'offset_ns': 1, 'pause': 'freeze'}) == ['clock-offset', 'clock-pause']
    assert features({'start_ns': 1, 'pause': 'freeze'}) == ['clock-start', 'clock-pause']
    assert features(None) == []


@pytest.mark.parametrize('source', ['cache', 'snapshot'])
def test_restores_choose_their_own_clock(monkeypatch, source):
    saved = definition(clock_offset=-3600)['spec']
    original = copy.deepcopy(saved)

    class Connection:
        def call(self, operation, **kwargs):
            assert operation == 'snapshot_spec'
            return {'reference': 'snap-pinned', 'spec': saved}

        def close(self):
            pass

    monkeypatch.setattr('sandweave.sandbox.sandbox.connect', lambda target: Connection())
    assert 'clock' not in definition(**{source: 'saved'})['spec']
    restored = definition(**{source: 'saved'}, clock_start='2030-01-01T00:00:00Z', clock_pause='continue')
    assert restored['spec']['clock'] == {'start_ns': 1893456000 * 10**9, 'pause': 'continue'}
    assert saved == original


def test_cli_options():
    options = creation(parser().parse_args(['create', '--clock-offset', '-3600', '--clock-pause', 'continue']))
    assert options['clock_offset'] == -3600 and options['clock_pause'] == 'continue'
    options = creation(parser().parse_args(['run', '--clock-start', '2025-03-01T00:00:00Z', '--', 'date']))
    assert options['clock_start'] == '2025-03-01T00:00:00Z'
    with pytest.raises(SystemExit):
        parser().parse_args(['create', '--clock-offset', '1', '--clock-start', '2025-03-01T00:00:00Z'])
    with pytest.raises(SystemExit):
        parser().parse_args(['create', '--clock-pause', 'stop'])


def test_information_reports_configured_clock():
    spec = definition(clock_offset=timedelta(hours=-2))['spec']
    record = {'id': 'sw-1', 'state': 'ready', 'spec': spec}
    assert summarize(record)['clock'] == {'offset_seconds': -7200.0, 'pause': 'freeze'}
    assert 'clock' not in summarize({**record, 'spec': definition()['spec']})
