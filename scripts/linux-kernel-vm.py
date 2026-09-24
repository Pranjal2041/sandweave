#!/usr/bin/env python3
"""Boot a disposable, real Linux 5.4 host under unprivileged QEMU TCG for SDK tests.

QEMU is test infrastructure only, not part of the Sandweave runtime.
The tools directory contains extracted Debian qemu-system-x86, qemu-utils,
seabios and genisoimage packages; tools-image provides their matching libc.
"""
import argparse
import json
from pathlib import Path
import shutil
import socket
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--tools', type=Path, required=True)
    parser.add_argument('--tools-image', type=Path, required=True)
    parser.add_argument('--memory-gib', type=int, default=16,
                        help='VM RAM; the default also admits the GNOME template')
    args = parser.parse_args()
    if args.memory_gib < 1:
        parser.error('--memory-gib must be positive')
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(root)
    if disk.free - 80 * 1024**3 < .15 * disk.total:
        parser.error('VM needs an 80 GiB growth budget while keeping 15% free')
    if (root / 'vm.json').exists():
        parser.error('this directory already owns a VM')
    key = root / 'id_ed25519'
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
    (root / 'user-data').write_text('''#cloud-config
users:
  - name: tester
    uid: 1000
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    ssh_authorized_keys:
      - ''' + key.with_suffix('.pub').read_text().strip() + '\n')
    (root / 'meta-data').write_text('instance-id: sandweave-linux54\nlocal-hostname: sandweave-linux54\n')
    container = ['apptainer', 'exec', '--userns', '--contain', '--cleanenv', '--no-home',
                 '--bind', str(root) + ':/vm', '--bind', str(args.image.resolve()) + ':/base.img:ro',
                 '--bind', str(args.tools.resolve()) + ':/qtools:ro', str(args.tools_image.resolve()),
                 'env', 'LD_LIBRARY_PATH=/qtools/usr/lib/x86_64-linux-gnu',
                 'PATH=/qtools/usr/bin:/usr/bin:/bin']
    subprocess.run([*container, 'qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2',
                    '-b', '/base.img', '/vm/disk.qcow2', '64G'], check=True)
    subprocess.run([*container, 'genisoimage', '-quiet', '-output', '/vm/seed.iso',
                    '-volid', 'cidata', '-joliet', '-rock', '/vm/user-data', '/vm/meta-data'], check=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    command = [*container, 'qemu-system-x86_64', '-machine', 'q35,accel=tcg',
               # A consistent 64-bit CPU model is required by Mesa's LLVM JIT.
               # Some QEMU versions identify "max" as a 32-bit AMD model.
               '-cpu', 'Nehalem', '-smp', '4', '-m', str(args.memory_gib * 1024), '-nodefaults',
               '-L', '/qtools/usr/share/qemu', '-bios', '/qtools/usr/share/seabios/bios-256k.bin',
               '-drive', 'file=/vm/disk.qcow2,if=virtio,format=qcow2,discard=unmap',
               '-drive', 'file=/vm/seed.iso,if=virtio,format=raw,readonly=on',
               '-netdev', f'user,id=net,hostfwd=tcp:127.0.0.1:{port}-:22',
               '-device', 'virtio-net-pci,netdev=net,romfile=',
               '-display', 'none', '-monitor', 'none', '-serial', 'file:/vm/serial.log',
               '-qmp', 'unix:/vm/qmp.sock,server=on,wait=off']
    with (root / 'qemu.log').open('wb') as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    receipt = {'pid': process.pid, 'port': port, 'key': str(key), 'command': command}
    (root / 'vm.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(f'VM launcher {process.pid}; SSH: ssh -i {key} -p {port} tester@127.0.0.1')


if __name__ == '__main__':
    main()
