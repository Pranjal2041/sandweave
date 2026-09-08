#!/usr/bin/env python3
"""Failure and ownership tests for the VR observation ring and input protocol."""
import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from vr_input import Packet, update
from vr_stream import FrameRing, VRStream, FrameRecorder, Frame, RecordedFrames


class VRStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def publish(self, ring, sequence, value, complete=True):
        offset = 256 + ((sequence-1) % ring.slots)*ring.slot_bytes
        struct.pack_into('<8Q', ring.map, offset, sequence*2 + (not complete), sequence,
                         sequence+100, 400, 100, 200, 300, 0)
        ring.map[offset+64:offset+ring.slot_bytes] = bytes([value])*(ring.slot_bytes-64)
        struct.pack_into('<Q', ring.map, 32, sequence)

    def test_guest_cannot_resize_donated_frame_memory(self):
        ring = FrameRing(self.path/'ring', 16, 16)
        self.addCleanup(ring.close)
        with self.assertRaises(PermissionError):
            os.ftruncate(ring.fd, 0)
        with self.assertRaises(PermissionError):
            os.ftruncate(ring.fd, ring.size+4096)

    def test_reader_skips_overwritten_frames_and_owns_pixels(self):
        ring = FrameRing(self.path/'ring', 16, 16, slots=2)
        self.addCleanup(ring.close)
        for i in range(1, 5):
            self.publish(ring, i, i)
        frame = ring.latest(after=1, timeout=0)
        self.assertEqual((frame.sequence, frame.compositor_frame), (4, 104))
        self.publish(ring, 6, 6)
        self.assertEqual(frame.rgba, bytes([4])*1024)
        self.assertEqual(ring.latest(after=4, timeout=0).rgba, bytes([6])*1024)

    def test_reader_rejects_incomplete_frame_and_no_new_frame(self):
        ring = FrameRing(self.path/'ring', 16, 16)
        self.addCleanup(ring.close)
        self.publish(ring, 1, 8, complete=False)
        with self.assertRaises(TimeoutError):
            ring.latest(timeout=0)
        self.publish(ring, 1, 8)
        with self.assertRaises(TimeoutError):
            ring.latest(after=1, timeout=0)

    def test_reader_rejects_frame_overwritten_during_copy(self):
        ring = FrameRing(self.path/'ring', 16, 16, slots=2)
        self.addCleanup(ring.close)
        self.publish(ring, 1, 8)
        original_unpack = struct.unpack_from
        def unpack(fmt, buf, offset=0):
            if offset == 256:
                self.publish(ring, 3, 9)
            return original_unpack(fmt, buf, offset)
        with mock.patch('vr_stream.struct.unpack_from', side_effect=unpack):
            with self.assertRaises(TimeoutError):
                ring.latest(timeout=0)

    def test_state_validation_is_atomic_and_normalizes_quaternions(self):
        packet = Packet()
        before = bytes(packet)
        with self.assertRaises(ValueError):
            update(packet, {'head': {'position': [1, 2, 3]}, 'right': {'trigger_value': 2}})
        self.assertEqual(bytes(packet), before)
        result = update(packet, {'head': {'orientation': [0, 0, 0, 2]},
                                 'right': {'thumbstick': [-1, .5], 'a_click': True}})
        self.assertEqual(list(result.head.center.orientation), [0, 0, 0, 1])
        self.assertTrue(result.right.a_click)
        for state in [{'head': {'orientation': [0]*4}}, {'right': {'position': [float('nan'), 0, 0]}},
                      {'left': {'trigger_click': 1}}, {'head': {'unknown': 1}}]:
            with self.assertRaises(ValueError):
                update(packet, state)

    def test_input_disconnect_poisons_channel_without_replaying(self):
        stream = VRStream('vr-test')
        stream.bridge = mock.Mock()
        stream.bridge.stdin.fileno.return_value = 44
        with mock.patch('vr_stream.os.set_blocking'), mock.patch('vr_stream.select.select', return_value=([], [44], [])), \
             mock.patch('vr_stream.os.write', side_effect=lambda fd, data: len(data)) as write, \
             mock.patch.object(stream, '_reply', side_effect=ConnectionError('closed')):
            with self.assertRaises(ConnectionError):
                stream.input({'right': {'trigger_click': True}})
            with self.assertRaisesRegex(RuntimeError, 'cannot be replayed'):
                stream.input({'right': {'trigger_click': True}})
            self.assertEqual(write.call_count, 1)

    def test_recorder_overflow_is_counted_without_blocking(self):
        entered, release = threading.Event(), threading.Event()
        original = Path.write_bytes
        def slow_write(path, data):
            entered.set()
            release.wait(timeout=3)
            return original(path, data)
        with mock.patch.object(Path, 'write_bytes', slow_write):
            recorder = FrameRecorder(self.path/'frames', capacity=1, workers=1)
            frame = Frame(1, 1, 4, 1, 2, 3, 5, 1, 16, 16, bytes(1024))
            try:
                recorder.submit(frame)
                self.assertTrue(entered.wait(timeout=3))
                recorder.submit(frame)
                recorder.submit(frame)
                self.assertEqual(recorder.dropped, 1)
                self.assertEqual(recorder.queue.qsize(), 1)
            finally:
                release.set()
                recorder.close()

    def test_recording_roundtrip_preserves_pixels_and_timestamps(self):
        recorder = FrameRecorder(self.path/'frames')
        frame = Frame(1, 1, 4, 1, 2, 3, 5, 1, 16, 16, bytes(range(256))*4)
        recorder.submit(frame)
        recorder.close()
        recording = RecordedFrames(self.path/'frames')
        self.assertEqual(len(recording), 1)
        self.assertEqual(recording[0], frame)
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            recorder.submit(frame)

    def test_background_recorder_propagates_storage_failure(self):
        recorder = FrameRecorder(self.path/'frames')
        frame = Frame(1, 1, 4, 1, 2, 3, 5, 1, 16, 16, bytes(1024))
        with mock.patch.object(Path, 'write_bytes', side_effect=OSError('disk failure')):
            recorder.submit(frame)
            with self.assertRaisesRegex(RuntimeError, 'disk failure'):
                recorder.close()


if __name__ == '__main__':
    unittest.main()
