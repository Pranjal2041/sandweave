"""Experimental CUDA 13.3 MPS client handshake gateway (no management commands)."""
import array
import os
from pathlib import Path
import socket
import struct
import threading


def packet(sock, expected, rights=False):
    data, ancillary, flags, _ = sock.recvmsg(64, 4096, socket.MSG_CMSG_CLOEXEC)
    fds = []
    try:
        for level, kind, value in ancillary:
            if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
                numbers = array.array('i')
                numbers.frombytes(value)
                fds.extend(numbers)
            elif (level, kind) != (socket.SOL_SOCKET, socket.SCM_CREDENTIALS):
                raise ValueError('unexpected ancillary message')
        if data != expected or flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ValueError('unexpected or truncated MPS handshake')
        if len(fds) != int(rights):
            raise ValueError('unexpected MPS descriptor count')
        return fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def relay(client, upstream):
    """Pass only a CUDA connection FD; the data path bypasses this gateway."""
    with client, socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
        client.settimeout(5)
        server.settimeout(5)
        _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            raise ValueError('MPS client belongs to another host user')
        # Validate the entire client request before contacting the controller.
        client.sendall(b'OUTBHELL\0')
        packet(client, b'OUTBCRED\0')
        packet(client, struct.pack('<I', 2))
        server.connect(str(upstream))
        packet(server, b'OUTBHELL\0')
        server.sendmsg([b'OUTBCRED\0'], [(socket.SOL_SOCKET, socket.SCM_CREDENTIALS,
                       struct.pack('3i', os.getpid(), os.getuid(), os.getgid()))])
        server.sendall(struct.pack('<I', 2))
        fds = packet(server, b'OUTBCUFD\0', rights=True)
        try:
            client.sendmsg([b'OUTBCUFD\0'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                                           array.array('i', fds))])
        finally:
            for fd in fds:
                os.close(fd)
        packet(client, struct.pack('<I', 0))
        server.sendall(struct.pack('<I', 0))


def serve(path, upstream):
    path = Path(path)
    slots = threading.BoundedSemaphore(16)

    def handle(client):
        try:
            relay(client, upstream)
        except (OSError, ValueError):
            client.close()  # Includes libcuda's connect/read-hello/disconnect probe.
        finally:
            slots.release()

    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
        listener.bind(str(path))
        listener.listen(16)
        # The parent service directory is host-user-private. Guest UIDs map to
        # this host user, but see imported file ownership as overflow UID.
        os.chmod(path, 0o666)
        try:
            while True:
                client, _ = listener.accept()
                if not slots.acquire(blocking=False):
                    client.close()
                    continue
                threading.Thread(target=handle, args=(client,), daemon=True).start()
        finally:
            path.unlink(missing_ok=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('socket', type=Path)
    parser.add_argument('upstream', type=Path)
    args = parser.parse_args()
    serve(args.socket, args.upstream)
