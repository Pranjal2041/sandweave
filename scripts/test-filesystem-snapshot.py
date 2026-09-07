#!/usr/bin/env python3
"""Check cold-snapshot mount coverage and boot staging."""
import unittest
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

if __name__ == '__main__': unittest.main()
