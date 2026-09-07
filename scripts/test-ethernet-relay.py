#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import socket
import struct
import threading
import unittest

spec = importlib.util.spec_from_file_location('relay', Path(__file__).with_name('ethernet-relay.py'))
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.guest, packet = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.passt, stream = socket.socketpair()
        self.errors = []
        self.guest.settimeout(2)
        self.passt.settimeout(2)

        def run():
            with packet, stream:
                try:
                    relay.relay(packet, stream)
                except Exception as exc:
                    self.errors.append(exc)
        self.thread = threading.Thread(target=run)
        self.thread.start()

    def tearDown(self):
        self.guest.close()
        self.passt.close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive(), 'relay hung after disconnect')

    def test_bidirectional_fragmented_and_coalesced_frames(self):
        frames = [bytes([x]) * (14+x*17) for x in range(1, 20)]
        for frame in frames:
            self.guest.sendall(frame)
        for frame in frames:
            length, = struct.unpack('!I', relay.read_exact(self.passt, 4))
            self.assertEqual(relay.read_exact(self.passt, length), frame)
        wire = b''.join(struct.pack('!I', len(f)) + f for f in frames)
        # Cut in the middle of length prefixes and payloads; also combine frames.
        for start, end in [(0, 1), (1, 3), (3, 38), (38, len(wire))]:
            self.passt.sendall(wire[start:end])
        for frame in frames:
            self.assertEqual(self.guest.recv(9014), frame)
        self.assertFalse(self.errors)

    def test_invalid_length_closes_connection(self):
        self.passt.sendall(struct.pack('!I', 9015))
        self.thread.join(2)
        self.assertTrue(self.errors)
        self.assertEqual(self.guest.recv(100), b'')

    def test_truncated_payload_closes_connection(self):
        self.passt.sendall(struct.pack('!I', 100) + b'partial')
        self.passt.shutdown(socket.SHUT_WR)
        self.thread.join(2)
        self.assertTrue(self.errors)


if __name__ == '__main__':
    unittest.main()
