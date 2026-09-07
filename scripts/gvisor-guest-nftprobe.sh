#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import socket, struct

def message(kind, flags, seq, data):
    return struct.pack('=IHHII', 16+len(data), kind, flags, seq, 0)+data

for label, order in [('network', '!H'), ('legacy-host', '=H')]:
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 12) as sock:
        sock.settimeout(3)
        sock.bind((0,0))
        nfgen = bytes([socket.AF_INET, 0]) + struct.pack(order, 10)
        begin = message(16,1,1,nfgen)
        name = ('engine_probe_' + label.replace('-','_')).encode()+b'\0'
        attr = struct.pack('=HH', 4+len(name),1)+name
        attr += bytes((-len(attr))%4)
        create = message(10<<8,0x605,2,bytes([socket.AF_INET,0,0,0])+attr)
        end = message(17,1,3,nfgen)
        sock.sendto(begin+create+end,(0,0))
        while True:
            data = sock.recv(65536)
            found = False
            while len(data)>=16:
                length,kind,flags,seq,pid = struct.unpack_from('=IHHII',data)
                if kind==2:
                    error, = struct.unpack_from('=i',data,16)
                    print(f'{label}: seq={seq} errno={-error}',flush=True)
                    if error:
                        raise SystemExit(1)
                    if seq==2: found=True
                data=data[(length+3)&~3:]
            if found: break
print('NFT_BATCH_COMPAT_PASS')
PY
