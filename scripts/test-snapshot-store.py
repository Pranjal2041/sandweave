#!/usr/bin/env python3
"""Exercise asynchronous verification, corruption detection and legacy metadata."""
import copy
import fcntl
import json
from pathlib import Path
import shutil
import socket
import tempfile
import time
import unittest
from unittest import mock

import runtime_store
import snapshot_store


class SnapshotStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.lab = Path(self.temporary.name)
        (self.lab / 'tools').mkdir()
        binary = self.lab / 'binary'
        binary.write_bytes(b'runtime')
        self.runtime = runtime_store.publish(self.lab, {'runsc': binary})
        (self.lab / 'images').mkdir()
        (self.lab / 'images/base').write_bytes(b'immutable base')
        self.source = self.lab / 'source'
        self.source.mkdir()
        payloads = {'checkpoint.img': b'kernel', 'pages.img': b'memory and files',
                    'lab-spec.json': b'{}', 'fixtures.tar': b'fixtures',
                    'launch-settings.json': json.dumps({'runtime': self.runtime}).encode()}
        for name, data in payloads.items():
            (self.source / name).write_bytes(data)
        self.snapshot = self.lab / 'snapshot'
        shutil.copytree(self.source, self.snapshot)
        self.manifest = {'format': 2, 'snapshot_id': 'test-snapshot', 'runtime': self.runtime,
                         'base_image': {'path': 'images/base', 'size': 14},
                         'files': {name: {'size': len(data)} for name, data in payloads.items()},
                         'verification_source': {'hostname': socket.gethostname(), 'path': str(self.source),
                             'base_path': str(self.lab / 'images/base'),
                             'base_stat': snapshot_store.signature(self.lab / 'images/base'),
                             'files': {name: snapshot_store.signature(self.source / name) for name in payloads}}}
        snapshot_store.write_json(self.snapshot / 'snapshot-manifest.json', self.manifest)
        snapshot_store.pending(self.snapshot, self.manifest)

    def test_filesystem_manifest_requires_every_mount_archive(self):
        self.manifest.update(kind='filesystem', filesystem={'mounts': [{'file': 'mount-0.tar'}]})
        self.manifest['files']['rootfs-upper.tar'] = {'size': 4}
        (self.snapshot / 'rootfs-upper.tar').write_bytes(b'root')
        snapshot_store.write_json(self.snapshot / 'snapshot-manifest.json', self.manifest)
        with self.assertRaisesRegex(ValueError, 'required files'):
            snapshot_store.inspect(self.lab, self.snapshot)
        self.manifest['files']['mount-0.tar'] = {'size': 5}
        (self.snapshot / 'mount-0.tar').write_bytes(b'mount')
        snapshot_store.write_json(self.snapshot / 'snapshot-manifest.json', self.manifest)
        with mock.patch.object(runtime_store, 'digest', side_effect=AssertionError('blocking hash')):
            snapshot_store.inspect(self.lab, self.snapshot)
        (self.snapshot / 'mount-0.tar').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'size mismatch'):
            snapshot_store.inspect(self.lab, self.snapshot)

    def test_pending_restore_never_hashes_payloads(self):
        with mock.patch.object(runtime_store, 'digest', side_effect=AssertionError('blocking hash')):
            snapshot_store.inspect(self.lab, self.snapshot)

    def test_detached_verifier_does_not_block_inspection(self):
        with (self.snapshot / '.verification.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            child = snapshot_store.start_verification(self.lab, self.snapshot)
            with mock.patch.object(runtime_store, 'digest', side_effect=AssertionError('blocking hash')):
                snapshot_store.inspect(self.lab, self.snapshot)
            self.assertEqual(json.loads((self.snapshot / 'verification.json').read_text())['status'], 'pending')
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = json.loads((self.snapshot / 'verification.json').read_text())
                if result['status'] in ('passed', 'failed'):
                    break
                time.sleep(.02)
            self.assertEqual(result['status'], 'passed', result)
        finally:
            try:
                child.wait(2)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.wait()

    def test_verifies_copy_and_then_works_without_local_source(self):
        result = snapshot_store.verify(self.lab, self.snapshot)
        self.assertEqual(result['status'], 'passed', result)
        shutil.rmtree(self.source)
        result = snapshot_store.verify(self.lab, self.snapshot)
        self.assertEqual(result['status'], 'passed', result)

    def test_same_size_copy_corruption_fails_and_blocks_restore(self):
        (self.snapshot / 'pages.img').write_bytes(b'Memory and files')
        self.assertEqual(snapshot_store.verify(self.lab, self.snapshot)['status'], 'failed')
        with self.assertRaisesRegex(ValueError, 'verification failed'):
            snapshot_store.inspect(self.lab, self.snapshot)
        shutil.copy2(self.source / 'pages.img', self.snapshot / 'pages.img')
        self.assertEqual(snapshot_store.verify(self.lab, self.snapshot)['status'], 'passed')
        snapshot_store.inspect(self.lab, self.snapshot)

    def test_corruption_after_initial_verification_is_detected(self):
        self.assertEqual(snapshot_store.verify(self.lab, self.snapshot)['status'], 'passed')
        (self.snapshot / 'pages.img').write_bytes(b'Memory and files')
        self.assertEqual(snapshot_store.verify(self.lab, self.snapshot)['status'], 'failed')

    def test_failed_snapshot_stays_blocked_during_retry(self):
        (self.snapshot / 'pages.img').write_bytes(b'Memory and files')
        self.assertEqual(snapshot_store.verify(self.lab, self.snapshot)['status'], 'failed')
        shutil.copy2(self.source / 'pages.img', self.snapshot / 'pages.img')
        original = runtime_store.digest

        def check_retry(path):
            with self.assertRaisesRegex(ValueError, 'verification failed'):
                snapshot_store.inspect(self.lab, self.snapshot)
            return original(path)

        with mock.patch.object(runtime_store, 'digest', side_effect=check_retry):
            result = snapshot_store.verify(self.lab, self.snapshot)
        self.assertEqual(result['status'], 'passed', result)
        snapshot_store.inspect(self.lab, self.snapshot)

    def test_source_changed_before_worker_started(self):
        (self.source / 'pages.img').write_bytes(b'Memory and files')
        result = snapshot_store.verify(self.lab, self.snapshot)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('captured source changed', result['error'])

    def test_source_changed_during_hash(self):
        original = runtime_store.digest

        def changing_digest(path):
            result = original(path)
            if Path(path) == self.source / 'pages.img':
                Path(path).write_bytes(b'Memory and files')
            return result

        with mock.patch.object(runtime_store, 'digest', side_effect=changing_digest):
            result = snapshot_store.verify(self.lab, self.snapshot)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('changed during verification', result['error'])

    def test_truncation_rejected_without_hash(self):
        (self.snapshot / 'pages.img').write_bytes(b'partial')
        with self.assertRaisesRegex(ValueError, 'size mismatch'):
            snapshot_store.inspect(self.lab, self.snapshot)

    def test_runtime_identity_and_payload_checked_at_separate_stages(self):
        runtime = self.lab / self.runtime['path']
        (runtime / 'runsc').write_bytes(b'Runtime')
        snapshot_store.inspect(self.lab, self.snapshot)
        self.assertIn('runtime digest mismatch', snapshot_store.verify(self.lab, self.snapshot)['error'])
        snapshot_store.write_json(runtime / 'manifest.json', {'runsc': 'wrong build'})
        with self.assertRaisesRegex(ValueError, 'runtime manifest'):
            snapshot_store.inspect(self.lab, self.snapshot)

    def test_legacy_snapshot(self):
        manifest = copy.deepcopy(self.manifest)
        manifest['format'] = 1
        manifest.pop('snapshot_id')
        manifest.pop('verification_source')
        for name, info in manifest['files'].items():
            info['sha256'] = runtime_store.digest(self.snapshot / name)
        manifest['base_image']['sha256'] = runtime_store.digest(self.lab / 'images/base')
        snapshot_store.write_json(self.snapshot / 'snapshot-manifest.json', manifest)
        with mock.patch.object(runtime_store, 'digest', side_effect=AssertionError('blocking hash')):
            snapshot_store.inspect(self.lab, self.snapshot)
        self.assertEqual(snapshot_store.verify(self.lab, self.snapshot)['status'], 'passed')

    def test_paths_cannot_escape_snapshot(self):
        self.manifest['files']['../binary'] = {'size': 7}
        snapshot_store.write_json(self.snapshot / 'snapshot-manifest.json', self.manifest)
        with self.assertRaisesRegex(ValueError, 'invalid snapshot dependency'):
            snapshot_store.inspect(self.lab, self.snapshot)

    def test_restore_reuses_only_unchanged_local_capture(self):
        local = self.lab / 'local'
        capture = local / 'gvisor/checkpoints/capture'
        capture.parent.mkdir(parents=True)
        self.source.rename(capture)
        self.manifest['verification_source']['path'] = str(capture)
        # Moving the directory preserves the recorded payload identities.
        snapshot_store.write_json(capture / 'snapshot-manifest.json', self.manifest)
        self.assertEqual(snapshot_store.restore_path(local, self.snapshot, self.manifest), capture)
        (capture / 'pages.img').write_bytes(b'Memory and files')
        self.assertEqual(snapshot_store.restore_path(local, self.snapshot, self.manifest), self.snapshot)

    def test_restore_ignores_source_on_other_node_or_outside_local_store(self):
        self.assertEqual(snapshot_store.restore_path(self.lab / 'local', self.snapshot, self.manifest), self.snapshot)
        self.manifest['verification_source']['hostname'] = 'different-node'
        self.assertEqual(snapshot_store.restore_path(self.lab, self.snapshot, self.manifest), self.snapshot)


if __name__ == '__main__':
    unittest.main()
