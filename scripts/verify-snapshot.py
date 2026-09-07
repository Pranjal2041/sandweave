#!/usr/bin/env python3
"""Verify a snapshot explicitly, or finish its background post-save verification."""
import argparse
import json
import os
from pathlib import Path

import snapshot_store

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--lab', type=Path, default=Path(__file__).resolve().parent.parent)
p.add_argument('snapshot', type=Path)
a = p.parse_args()
os.nice(10)
result = snapshot_store.verify(a.lab.resolve(), a.snapshot)
print(json.dumps(result, indent=2), flush=True)
raise SystemExit(0 if result['status'] == 'passed' else 1)
