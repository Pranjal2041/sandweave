#!/usr/bin/env python3
"""Fast I/O failure, synchronization and validation tests without a sandbox."""
import mmap
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from environment import EnvironmentManager
import environment_control as control
from fast_io import Bridge, FastIOClient, events_for_action, io_lock, HEADER, FRAME_BYTES


class FastIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        lab = Path(self.temp.name)
        (lab/'runs').mkdir()
        (lab/'runs/local-path.txt').write_text(str(lab/'local'))
        self.manager = EnvironmentManager(lab)
        self.client = FastIOClient('test', manager=self.manager)
        self.addCleanup(self.client.close)

    def test_invalid_late_action_never_sends_an_earlier_action(self):
        with mock.patch('fast_io._rpc') as send:
            with self.assertRaisesRegex(ValueError, 'unknown mouse'):
                self.client.action([{'mouse': {'left_click': [1,2]}}, {'mouse': {'typo': [1,2]}}])
            send.assert_not_called()

    def test_action_disconnect_is_never_retried(self):
        with mock.patch('fast_io._rpc', side_effect=ConnectionResetError) as send:
            with self.assertRaisesRegex(RuntimeError, 'outcome unknown; not retried'):
                self.client.action({'mouse': {'left_click': [1,2]}})
            self.assertEqual(send.call_count, 1)

    def test_full_guest_pipe_times_out_and_poisoned_channel_cannot_replay(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        stream = os.fdopen(write_fd, 'wb', buffering=0)
        self.addCleanup(stream.close)
        os.set_blocking(write_fd, False)
        while True:
            try:
                os.write(write_fd, b'x'*4096)
            except BlockingIOError:
                break
        bridge = Bridge.__new__(Bridge)
        bridge.closed = bridge.broken = False
        bridge.process = SimpleNamespace(stdin=stream)
        with mock.patch('fast_io.select.select', return_value=([], [], [])):
            with self.assertRaisesRegex(TimeoutError, 'write timed out'):
                bridge.request(2, [(1,0,1,1)])
        self.assertTrue(bridge.broken)
        with self.assertRaisesRegex(RuntimeError, 'previous action outcome is unknown'):
            bridge.request(2, [(1,0,1,1)])

    def test_shared_io_and_exclusive_lifecycle_locks(self):
        with io_lock(self.manager, 'test'), io_lock(self.manager, 'test'):
            with self.assertRaisesRegex(RuntimeError, 'in progress'):
                control.acquire_lock(self.manager.local, 'test')
        with control.acquire_lock(self.manager.local, 'test'):
            with self.assertRaisesRegex(RuntimeError, 'in progress'), io_lock(self.manager, 'test'):
                pass
            with io_lock(self.manager, 'another'):
                pass

    def test_pause_aborts_if_shared_frame_cannot_detach(self):
        with mock.patch.object(self.manager, 'status', return_value={'status':'running'}), \
             mock.patch('environment.control.release_cpu', return_value=[]), \
             mock.patch('environment.fast_io.detach', side_effect=RuntimeError('detach failed')), \
             mock.patch.object(self.manager, '_run') as run:
            with self.assertRaisesRegex(RuntimeError, 'detach failed'):
                self.manager.pause('test')
            run.assert_not_called()

    def test_retry_cannot_hide_an_abnormal_helper_exit(self):
        bridge = Bridge.__new__(Bridge)
        bridge.closed = bridge.broken = True
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, 'uncertain shared-buffer state'):
                bridge.close()

    def test_rejected_detach_preflight_preserves_the_channel_for_retry(self):
        bridge = Bridge.__new__(Bridge)
        bridge.closed = bridge.broken = False
        bridge.ready = True
        bridge.process = mock.Mock()
        bridge.process.poll.return_value = None
        with mock.patch.object(bridge, 'request', side_effect=ValueError('release temporary Unicode keys')):
            with self.assertRaisesRegex(ValueError, 'release temporary Unicode keys'):
                bridge.close()
        bridge.process.stdin.write.assert_not_called()
        bridge.process.stdin.close.assert_not_called()
        self.assertFalse(bridge.closed)
        self.assertFalse(bridge.broken)

    def test_guest_cannot_overrun_host_image_dimensions(self):
        self.client._map = mmap.mmap(-1, FRAME_BYTES)
        self.client._generation = 'test'
        HEADER.pack_into(self.client._map, 0, 2, 32768, 32768, 32768*4, 0, 1, 2, 0)
        with self.assertRaisesRegex(ValueError, 'invalid shared frame dimensions'):
            self.client._image({'generation':'test', 'frame_sequence':2})

    def test_incomplete_or_replaced_frame_is_rejected(self):
        self.client._map = mmap.mmap(-1, FRAME_BYTES)
        self.client._generation = 'test'
        for sequence in (3, 4):
            HEADER.pack_into(self.client._map, 0, sequence, 2, 2, 8, 0, 1, 2, 0)
            self.assertIsNone(self.client._image({'generation':'test', 'frame_sequence':2}))

    def test_unimplemented_backend_does_not_start_a_service(self):
        with self.assertRaisesRegex(ValueError, 'Wayland remains deferred'):
            FastIOClient('test', manager=self.manager, backend='wayland')

    def test_text_and_gesture_size_are_bounded_before_transport(self):
        with self.assertRaises(ValueError):
            events_for_action({'keyboard': {'text':'a'*2049}})
        with self.assertRaises(ValueError):
            events_for_action({'mouse': {'scroll':1001}})
        with self.assertRaises(ValueError):
            events_for_action({'mouse': {'move':[float('nan'), 0]}})

    def test_extended_buttons_and_diagonal_wheel_preserve_order(self):
        events = events_for_action([{'mouse': {'back_click': [10,20]}},
                                   {'mouse': {'forward_click': [10,20]}},
                                   {'mouse': {'scroll': {'dx': 2, 'dy': -1}}}])
        self.assertEqual([(e[0], e[1]) for e in events if e[0] != 1],
                         [(2,8),(3,8),(2,9),(3,9),(2,7),(3,7),(2,7),(3,7),(2,4),(3,4)])
        self.assertEqual(events_for_action({'mouse': {'scroll': {'dx': 0, 'dy': 0}}}), [])

    def test_invalid_scroll_axis_rejects_the_whole_action(self):
        for value in ({'dx': True}, {'dy': 1001}, {'dx': 1.5}, {'dy': 1, 'typo': 2}):
            with self.subTest(value=value), mock.patch('fast_io._rpc') as send:
                with self.assertRaises(ValueError):
                    self.client.action([{'mouse': {'back_click': [1,2]}}, {'mouse': {'scroll': value}}])
                send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
