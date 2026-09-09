from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from sandweave.sandbox.timings import collect, measure


def test_concurrent_startups_keep_separate_timings_and_record_failed_phases():
    barrier = threading.Barrier(2)

    def request(name):
        with collect() as values:
            with pytest.raises(RuntimeError):
                with measure(name):
                    barrier.wait(timeout=5)
                    raise RuntimeError('startup failed')
        # Attaching to an existing desktop must not rewrite its startup times.
        with measure('reattach_seconds'):
            pass
        return values

    with ThreadPoolExecutor(2) as executor:
        first, second = list(executor.map(request, ['first_seconds', 'second_seconds']))
    assert set(first) == {'first_seconds'} and first['first_seconds'] > 0
    assert set(second) == {'second_seconds'} and second['second_seconds'] > 0


def test_nested_collection_restores_outer_measurements():
    with collect() as outer:
        with measure('total_seconds'):
            with collect() as inner:
                with measure('child_seconds'):
                    pass
            with measure('parent_seconds'):
                pass
    assert set(outer) == {'total_seconds', 'parent_seconds'}
    assert set(inner) == {'child_seconds'}
    assert outer['total_seconds'] >= outer['parent_seconds'] >= 0
