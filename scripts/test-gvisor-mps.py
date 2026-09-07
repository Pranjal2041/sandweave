#!/usr/bin/env python3
"""CPU-only checks for MPS opt-in validation and the CUDA-only socket gateway."""
import array
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

import gvisor_mps as mps
import mps_client_gateway as gateway


class MPSTests(unittest.TestCase):
    def test_default_never_queries_driver(self):
        with patch.object(mps.gvisor_gpu, 'driver_version', side_effect=AssertionError):
            mps.validate(None, None, None)
            mps.validate(0, None, None)

    def test_invalid_options(self):
        for options in ((None, 1, None), (0, 0, None), (0, -1, 1024),
                        (0, None, 1024), (0, 1, 100)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                mps.validate(*options)
        with patch.object(mps.gvisor_gpu, 'driver_version', return_value='unknown'):
            with self.assertRaisesRegex(ValueError, 'qualified only'):
                mps.validate(0, 1, 1024)

    def test_reused_pid_is_not_owned(self):
        current = mps.identity(os.getpid())
        self.assertTrue(mps.alive(current))
        self.assertFalse(mps.alive({**current, 'starttime': '0'}))

    def test_management_requests_never_reach_controller(self):
        # Only command 2 is CUDA. Quit/list/partition operations cannot pass.
        for request in (0, 1, 3, 4, 255):
            with self.subTest(request=request):
                client, accepted = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                with client:
                    client.sendall(b'OUTBCRED\0')
                    client.sendall(struct.pack('<I', request))
                    with self.assertRaisesRegex(ValueError, 'unexpected'):
                        gateway.relay(accepted, '/does-not-exist/controller')

    def test_injected_descriptors_closed(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with left, right, tempfile.TemporaryFile() as file:
            count = len(list(Path('/proc/self/fd').iterdir()))
            left.sendmsg([b'OUTBCRED\0'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                         array.array('i', [file.fileno()]))])
            with self.assertRaisesRegex(ValueError, 'descriptor count'):
                gateway.packet(right, b'OUTBCRED\0')
            self.assertEqual(len(list(Path('/proc/self/fd').iterdir())), count)

    def test_cuda_fd_handoff_and_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            endpoint = str(Path(directory) / 'control')
            errors = []
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            upstream.bind(endpoint)
            upstream.listen(1)
            data_a, data_b = socket.socketpair()
            client, accepted = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)

            def controller():
                try:
                    connection, _ = upstream.accept()
                    with connection:
                        connection.settimeout(3)
                        connection.sendall(b'OUTBHELL\0')
                        gateway.packet(connection, b'OUTBCRED\0')
                        gateway.packet(connection, struct.pack('<I', 2))
                        connection.sendmsg([b'OUTBCUFD\0'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                                           array.array('i', [data_a.fileno()]))])
                        gateway.packet(connection, struct.pack('<I', 0))
                except Exception as error:
                    errors.append(error)

            def proxy():
                try:
                    gateway.relay(accepted, endpoint)
                except Exception as error:
                    errors.append(error)

            threads = [threading.Thread(target=controller), threading.Thread(target=proxy)]
            with upstream, data_a, data_b, client:
                for thread in threads:
                    thread.start()
                client.settimeout(3)
                gateway.packet(client, b'OUTBHELL\0')
                client.sendall(b'OUTBCRED\0')
                client.sendall(struct.pack('<I', 2))
                fd, = gateway.packet(client, b'OUTBCUFD\0', rights=True)
                with socket.socket(fileno=fd) as passed:
                    passed.sendall(b'CUDA data')
                    self.assertEqual(data_b.recv(9), b'CUDA data')
                client.sendall(struct.pack('<I', 0))
                for thread in threads:
                    thread.join(4)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
