#!/usr/bin/env python3
"""Check cold-snapshot mount coverage and boot staging."""
import unittest
from unittest import mock
from pathlib import Path
import filesystem_snapshot as fs

class FilesystemSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.spec = {'mounts': [{'destination': '/var/lib/docker', 'type': 'tmpfs', 'options': ['mode=0711']}], 'process': {}}
        self.mounts = '\n'.join([
            '1 0 0:1 / / rw - overlay none rw',
            '2 1 0:2 / /run rw - tmpfs none rw',
            '3 1 0:3 / /var/lib/docker rw - tmpfs none rw',
            '4 1 0:4 / /opt/engine-gpu ro - 9p none ro',
            '5 3 0:5 / /var/lib/docker/overlay2/test/merged rw - overlay none rw',
        ])
    def test_old_runtime_rejected_before_pause(self):
        with mock.patch.object(fs.subprocess, 'run', return_value=mock.Mock(stdout='old tar help', stderr='')) as call:
            with self.assertRaisesRegex(ValueError, 'has not been paused'):
                fs.capture(['runtime'], 'env', Path('/unused'), self.spec, Path('/lab'), Path('/local'))
            self.assertEqual(call.call_count, 1)
            self.assertNotIn('pause', call.call_args.args[0])

    def test_includes_separate_docker_mount(self):
        saved = fs.inventory(self.spec, self.mounts)
        self.assertEqual([x['destination'] for x in saved], ['/var/lib/docker'])
    def test_unknown_writable_mount_fails(self):
        with self.assertRaisesRegex(ValueError, 'unsupported writable mount'):
            fs.inventory(self.spec, self.mounts + '\n6 1 0:6 / /mnt/data rw - tmpfs none rw')
    def test_missing_expected_mount_fails(self):
        with self.assertRaisesRegex(ValueError, 'differs'):
            fs.inventory(self.spec, self.mounts.replace('3 1 0:3 / /var/lib/docker rw - tmpfs none rw\n', ''))
    def test_boot_defaults_to_init_and_preserves_explicit_argv(self):
        fs.prepare_boot(self.spec, [])
        self.assertEqual(self.spec['process']['args'][-1], '/sbin/init')
        fs.prepare_boot(self.spec, ['--', '/bin/program', 'a b', '$(literal)'])
        self.assertEqual(self.spec['process']['args'][-3:], ['/bin/program', 'a b', '$(literal)'])

    def test_slow_readiness_rpc_retries_before_releasing_init(self):
        guest = mock.Mock()
        guest.poll.return_value = None
        done = mock.Mock()
        done.is_set.return_value = False
        with mock.patch.object(fs.subprocess, 'run', side_effect=[
                fs.subprocess.TimeoutExpired(['runtime', 'read'], 10),
                mock.Mock(returncode=0), mock.Mock(returncode=0)]) as run:
            fs.finish_boot(['runtime'], 'env', Path('/lab/snapshot'),
                           {'filesystem': {'mounts': []}}, Path('/lab'),
                           Path('/local'), guest, done)
        self.assertEqual(run.call_args_list[0].args[0], run.call_args_list[1].args[0])
        self.assertEqual(run.call_args_list[2].args[0],
                         ['runtime', 'exec', 'env', 'touch', '/run/engine-fs-ready'])

    def test_slow_readiness_rpc_still_honors_startup_deadline(self):
        guest = mock.Mock()
        guest.poll.return_value = None
        done = mock.Mock()
        done.is_set.return_value = False
        with mock.patch.object(fs.time, 'monotonic', side_effect=[0, 1, 301]), \
             mock.patch.object(fs.subprocess, 'run', side_effect=
                               fs.subprocess.TimeoutExpired(['runtime', 'read'], 10)) as run:
            with self.assertRaisesRegex(TimeoutError, 'did not become ready'):
                fs.finish_boot(['runtime'], 'env', Path('/lab/snapshot'),
                               {'filesystem': {'mounts': []}}, Path('/lab'),
                               Path('/local'), guest, done)
        self.assertEqual(run.call_count, 1)

if __name__ == '__main__': unittest.main()
