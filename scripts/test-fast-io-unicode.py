#!/usr/bin/env python3
"""Exercise Unicode cache turnover through an unmodified harness probe."""
import argparse
from pathlib import Path

from autoharness.backend import GenericProbeAdapter
from autoharness.contracts import CatalogEntry, TypeText
from autoharness.driver import run_catalog
from cua_harness_adapter import GVisorXvncAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', help='fresh disposable fastio-harness-* name')
    args = parser.parse_args()
    backend = GVisorXvncAdapter(options={'name': args.name})
    probe = GenericProbeAdapter(backend, name='fastio-unicode-turnover')
    # Different uncased symbols force eviction; no inter-action sleeps or
    # probe reads are inserted into the sequence being tested.
    strings = [''.join(chr(0x4e00 + i*16 + j) for j in range(16)) for i in range(64)]
    entry = CatalogEntry(id='unicode_cache_turnover', actions=[TypeText(text=s) for s in strings],
                         expect={'check':'type_text', 'text':''.join(strings)}, reps=3)
    report = run_catalog(probe, [entry], Path('runs')/args.name)
    return 0 if report['totals']['pass'] == 1 else 1


if __name__ == '__main__':
    raise SystemExit(main())
