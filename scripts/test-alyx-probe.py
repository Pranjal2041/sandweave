#!/usr/bin/env python3
"""Regression checks for incremental probe logs and bounded output collection."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('alyx_probe', Path(__file__).with_name('alyx-probe.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class StreamTests(unittest.TestCase):
    def test_child_output_is_visible_before_exit_and_keeps_parent_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'output.log'
            child = """import pathlib, sys, time
print('child-first', flush=True)
time.sleep(.2)
assert pathlib.Path(sys.argv[1]).read_text() == 'parent-before\\nchild-first\\n'
print('child-last', flush=True)
sys.exit(7)
"""
            with path.open('w') as output, patch.object(sys, 'stdout', output):
                print('parent-before', flush=True)
                result = probe.run_streamed([sys.executable, '-c', child, str(path)], 5)
                print('parent-after', flush=True)
            self.assertEqual(result.returncode, 7)
            self.assertEqual(path.read_text(), 'parent-before\nchild-first\nchild-last\nparent-after\n')

    def test_silent_child_is_bounded(self):
        with tempfile.TemporaryFile(mode='w+') as output, patch.object(sys, 'stdout', output):
            with self.assertRaises(subprocess.TimeoutExpired):
                probe.run_streamed([sys.executable, '-c', 'import time; time.sleep(10)'], .1)


if __name__ == '__main__':
    unittest.main()
