"""CPU demand regressions independent of a host's size or process scheduling."""
import importlib.util
from pathlib import Path
import random

import pytest


def broker_module():
    spec = importlib.util.spec_from_file_location('cpu_demand_broker',
        Path(__file__).parents[1] / 'scripts/cpu_broker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


broker = broker_module()


def job(demand=None, weight=100, quota=None):
    result = broker.Job({'root': 1, 'weight': weight, 'quota': quota})
    result.demand = demand
    result.observed_demand = demand
    return result


def test_active_single_cpu_peer_lends_the_other_three_cpus():
    light, busy = job(1), job()
    assert broker.allocate_rates([light, busy], 4) == {light: 1, busy: 3}


def test_128_partially_busy_sandboxes_lend_to_one_busy_sandbox():
    light = [job(.01) for _ in range(127)]
    busy = job()
    rates = broker.allocate_rates([*light, busy], 64)
    assert rates[busy] == pytest.approx(62.73)
    assert sum(rates.values()) == pytest.approx(64)


def test_runnable_peer_reclaims_its_weighted_share():
    light, busy = job(1, weight=300), job()
    assert broker.allocate_rates([light, busy], 4) == {light: 1, busy: 3}
    light.demand = 8  # Runnable, even if the host has not given it CPU yet.
    assert broker.allocate_rates([light, busy], 4) == {light: 3, busy: 1}


def test_lending_keeps_explicit_quotas():
    light, capped, busy = job(.25), job(quota=1), job()
    assert broker.allocate_rates([light, capped, busy], 4) == {light: .25, capped: 1, busy: 2.75}
    assert broker.allocate_rates([capped], 4) == {capped: 1}


def test_unused_capacity_remains_available_for_new_work():
    a, b = job(.25), job(.25)
    assert broker.allocate_rates([a, b], 4) == {a: 2, b: 2}
    assert broker.allocate_rates([a], 64) == {a: 64}
    assert broker.allocate_rates([], 4) == {}


def test_capacity_and_quotas_hold_for_mixed_demand():
    rng = random.Random(617)
    for _ in range(200):
        capacity = rng.uniform(.1, 64)
        jobs = [job(rng.choice([None, 0., rng.random() * 16]),
                    rng.randint(1, 1000), rng.choice([None, rng.uniform(.01, 16)]))
                for _ in range(rng.randint(1, 128))]
        rates = broker.allocate_rates(jobs, capacity)
        assert sum(rates.values()) <= capacity + 1e-9
        assert all(rate >= -1e-9 for rate in rates.values())
        assert all(j.config['quota'] is None or rates[j] <= j.config['quota'] + 1e-9 for j in jobs)


def table(state='R', threads=1):
    return {1: {'state': state, 'threads': threads}}


def test_demand_uses_unthrottled_cpu_consumption_and_runnable_work(monkeypatch):
    a = job()
    a.update_demand(table(), 0, 0)
    a.update_demand(table(), .1, .1)
    assert a.demand == pytest.approx(1)
    # Low delivered CPU while runnable must not look like low demand.
    monkeypatch.setattr(broker, 'runnable_threads', lambda *args: set(range(1, 9)))
    a.update_demand(table(), .01, .2)
    a.update_demand(table(), .01, .31)
    assert a.demand == 8


def test_pause_does_not_turn_a_hungry_sandbox_into_a_low_demand_peer():
    a = job()
    a.update_demand(table(), 0, 0)
    a.update_demand(table(), .1, .1)
    a.paused = True
    a.update_demand(table('S'), 0, .2)
    assert a.demand is None
    a.paused = False
    a.update_demand(table(), .01, .22)
    assert a.demand == 1  # Retain the earlier unthrottled observation.


def test_persistently_runnable_processes_raise_demand_before_window_ends():
    a = job(1)
    a.demand_sample = 0
    a.members = {1, 2, 3}
    a.update_demand({i: {'state': 'R', 'threads': 1} for i in a.members}, .02, .02)
    a.update_demand({i: {'state': 'R', 'threads': 1} for i in a.members}, .02, .07)
    assert a.demand == 3


def test_unreadable_thread_sample_does_not_lend_its_entitlement(monkeypatch):
    a = job(1)
    a.demand_sample = 0
    monkeypatch.setattr(broker, 'runnable_threads', lambda *args: None)
    a.update_demand(table(), .01, .1)
    assert a.demand is None


def test_brief_runnable_burst_does_not_reserve_capacity_after_sleep():
    a = job(.1)
    a.demand_sample = 0
    a.update_demand(table(), .01, .02)
    assert a.demand == .1
    a.update_demand(table('S'), 0, .04)
    assert a.demand == .1


def test_interleaved_throttling_preserves_partial_observation_windows():
    a = job()
    a.update_demand(table('S'), 0, 0)
    for end in (.04, .10, .16):
        a.paused = False
        a.update_demand(table('S'), .004, end)
        if end < .16:
            a.paused = True
            a.update_demand(table('S'), 0, end + .02)
    assert a.demand == pytest.approx(.1)


def test_intermittent_share_can_bank_a_whole_accounting_tick():
    a = job(.07)
    for _ in range(5):
        a.refill(.25, .02)  # Idle gaps still accrue a bounded burst allowance.
    a.refill(.07, .02)
    a.credit -= 1 / broker.os.sysconf('SC_CLK_TCK')
    a.refill(.07, .02)
    assert a.credit > 0, 'a short wakeup must not trigger an artificial pause'


def test_idle_refill_does_not_forgive_quota_debt():
    a = job(quota=.1)
    a.credit = -.05
    a.refill(.1, .1)
    assert a.credit == pytest.approx(-.04)


def test_sleeping_process_leader_does_not_hide_runnable_threads(tmp_path, monkeypatch):
    process = tmp_path / '1/task'
    for tid, state in [(1, 'S'), (2, 'R'), (3, 'R')]:
        path = process / str(tid) / 'stat'
        path.parent.mkdir(parents=True)
        path.write_text(f'{tid} (name with ) parentheses) {state} 0 0')
    monkeypatch.setattr(broker, 'Path', lambda path: tmp_path / path.removeprefix('/proc/'))
    assert broker.runnable_threads(table('S', 3), {1}) == {2, 3}
    (process / '2/stat').unlink()
    assert broker.runnable_threads(table('S', 3), {1}) is None
