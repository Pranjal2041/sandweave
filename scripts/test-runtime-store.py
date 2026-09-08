#!/usr/bin/env python3
"""Candidate selection must preserve the default and reject altered binaries."""
import json
from pathlib import Path
import tempfile
import unittest

import runtime_store


class RuntimeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.lab = Path(self.temporary.name)
        (self.lab / 'tools').mkdir()
        source = self.lab / 'source'
        source.write_bytes(b'test runtime executable')
        self.descriptor = runtime_store.publish(self.lab, {'runsc': source})

    def test_selection_does_not_change_default(self):
        default = self.lab / 'tools/gvisor-socket/runtime.json'
        default.parent.mkdir()
        default.write_text('{"preserve": true}\n')
        self.assertEqual(runtime_store.from_directory(self.lab, self.descriptor['path']),
                         self.descriptor)
        self.assertEqual(json.loads(default.read_text()), {'preserve': True})

    def test_changed_binary_is_rejected(self):
        (self.lab / self.descriptor['path'] / 'runsc').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            runtime_store.from_directory(self.lab, self.descriptor['path'])

    def test_build_outside_store_is_rejected(self):
        outside = self.lab / 'outside'
        outside.mkdir()
        alias = self.lab / 'tools/runtime-builds/alias'
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'immutable build store'):
            runtime_store.from_directory(self.lab, alias)

    def test_binary_outside_build_is_rejected(self):
        binary = self.lab / self.descriptor['path'] / 'runsc'
        binary.unlink()
        binary.symlink_to(self.lab / 'source')
        with self.assertRaisesRegex(ValueError, 'missing or invalid runtime file'):
            runtime_store.from_directory(self.lab, self.descriptor['path'])


if __name__ == '__main__':
    unittest.main()
