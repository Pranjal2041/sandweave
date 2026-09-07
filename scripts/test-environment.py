#!/usr/bin/env python3
"""Lifecycle regressions that do not boot a sandbox."""
import json
import io
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import cpu_broker
import environment_control as control
import env
from environment import EnvironmentManager


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.lab = Path(self.temp.name)
        (self.lab / 'runs').mkdir()
        (self.lab / 'runs/local-path.txt').write_text(str(self.lab / 'local'))
        self.manager = EnvironmentManager(self.lab)

    def test_same_environment_operations_exclude_each_other(self):
        with control.acquire_lock(self.manager.local, 'test'):
            with self.assertRaisesRegex(RuntimeError, 'in progress'):
                control.acquire_lock(self.manager.local, 'test')
            with control.acquire_lock(self.manager.local, 'independent'):
                pass
        with control.acquire_lock(self.manager.local, 'test'):
            pass

    def test_checkpoint_can_inherit_only_its_environment_lock(self):
        with control.acquire_lock(self.manager.local, 'test') as lock:
            command = [sys.executable, '-c',
                'import environment_control as c,sys; '
                'h=c.acquire_lock(sys.argv[1],sys.argv[2],sys.argv[3]); h.close()',
                str(self.manager.local), 'test', str(lock.fileno())]
            subprocess.run(command, cwd=Path(__file__).parent, pass_fds=(lock.fileno(),), check=True)
            with self.assertRaisesRegex(RuntimeError, 'in progress'):
                control.acquire_lock(self.manager.local, 'test')
            with control.acquire_lock(self.manager.local, 'other'):
                with self.assertRaisesRegex(ValueError, 'another environment'):
                    control.acquire_lock(self.manager.local, 'other', lock.fileno())

    def test_checkpoint_preserves_user_pause_marker(self):
        directory = self.manager.local / 'gvisor/cpu-brokers/pool-test'
        directory.mkdir(parents=True)
        (directory / 'job-test.json').write_text('{}')
        marker = directory / 'job-test.json.suspend'
        marker.write_text('user pause')
        (directory / 'status.json').write_text(json.dumps({
            'time': time.time() + 1, 'jobs': {'job-test.json': {'paused': False}}}))
        with control.suspended_cpu(self.manager.local, 'test'):
            self.assertTrue(marker.exists())
        self.assertEqual(marker.read_text(), 'user pause')
        control.resume_cpu(self.manager.local, 'test')
        self.assertFalse(marker.exists())

    def test_failed_stop_save_never_terminates_and_restores_prior_state(self):
        for state in ('running', 'paused'):
            with self.subTest(state=state), mock.patch.object(self.manager, 'status', return_value={'status': state}), \
                 mock.patch.object(self.manager, '_pause'), mock.patch.object(self.manager, '_resume') as resume, \
                 mock.patch.object(self.manager, '_save', side_effect=RuntimeError('disk full')), \
                 mock.patch.object(self.manager, '_run') as run:
                with self.assertRaisesRegex(RuntimeError, 'disk full'):
                    self.manager.stop('test')
                self.assertEqual(resume.call_count, int(state == 'running'))
                run.assert_not_called()

    def test_gpu_live_capture_requires_explicit_experimental_mode(self):
        with control.acquire_lock(self.manager.local, 'test') as lock, \
             mock.patch.object(self.manager, 'status', return_value={'status': 'running', 'gpu': True}), \
             mock.patch.object(self.manager, '_run') as run:
            with self.assertRaisesRegex(ValueError, 'experimental-gpu-live'):
                self.manager._save('test', 'label', 'live', False, lock)
            run.assert_not_called()

    def test_existing_bundle_is_never_overwritten(self):
        bundle = self.manager._bundle('test')
        bundle.mkdir(parents=True)
        marker = bundle / 'user-data'
        marker.write_text('retained')
        with mock.patch.object(self.manager, '_run') as run:
            with self.assertRaisesRegex(ValueError, 'fresh name'):
                self.manager.start('test')
            run.assert_not_called()
        self.assertEqual(marker.read_text(), 'retained')

    def test_cli_preserves_launcher_options_and_literal_guest_arguments(self):
        with mock.patch.object(env, 'EnvironmentManager') as factory, mock.patch('sys.stdout', new_callable=io.StringIO):
            manager = factory.return_value
            manager.start.return_value = {'status': 'running'}
            env.main(['start', 'test', '--launch-options', '--memory-mib', '512', '--',
                      'sh', '-c', 'printf "%s" "$literal"'])
            manager.start.assert_called_once_with('test', options=['--memory-mib', '512'],
                                                  command=['sh', '-c', 'printf "%s" "$literal"'])

    def test_helper_ledger_excludes_shared_launcher_children(self):
        logs = self.manager._logs('test')
        logs.mkdir(parents=True)
        (logs / 'owned-processes.json').write_text(json.dumps({
            'launcher': 100, 'processes': [{'pid': 101, 'start': 2}]}))
        def table(pids):
            return {p: {'start': p - 99, 'state': 'S'} for p in pids}
        with mock.patch.object(cpu_broker, 'process_table', side_effect=table), \
             mock.patch.object(cpu_broker, 'discover_trees', return_value={}) as discover:
            self.manager._owned_members('test', {'pid': 100, 'start': 1}, None)
            discover.assert_called_once_with([101])

    def test_pid_reuse_guard_and_pidfd_signal(self):
        child = subprocess.Popen(['sleep', '30'])
        try:
            start = cpu_broker.process_table([child.pid])[child.pid]['start']
            self.manager._signal(child.pid, start - 1, signal.SIGKILL)
            self.assertIsNone(child.poll())
            self.manager._signal(child.pid, start, signal.SIGTERM)
            self.assertEqual(child.wait(timeout=3), -signal.SIGTERM)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()


if __name__ == '__main__':
    unittest.main()
