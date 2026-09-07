#!/bin/bash
set -euo pipefail
mkdir -p /mnt/cgroup-a /mnt/cgroup-b
mount -t cgroup -o none,name=gvm_probe none /mnt/cgroup-a
mkdir /mnt/cgroup-a/child /mnt/cgroup-a/sibling
python3 - <<'PY'
import ctypes, os, subprocess
from pathlib import Path
libc=ctypes.CDLL(None,use_errno=True)
def check(ret):
    if ret: raise OSError(ctypes.get_errno(),os.strerror(ctypes.get_errno()))
def entry(pid):
    lines=Path(f'/proc/{pid}/cgroup').read_text().splitlines()
    return next(line.split(':',2)[2] for line in lines if ':name=gvm_probe:' in line)
assert entry('self')=='/'
original=os.open('/proc/self/ns/cgroup',os.O_RDONLY)
parent=os.getppid()
Path('/mnt/cgroup-a/child/cgroup.procs').write_text(str(os.getpid()))
assert entry('self')=='/child'
check(libc.unshare(0x02000000|0x00020000))
assert entry('self')=='/'
assert entry(parent)=='/..'
subprocess.run(['mount','-t','cgroup','-o','name=gvm_probe','none','/mnt/cgroup-b'],check=True)
assert not Path('/mnt/cgroup-b/sibling').exists()
line=next(l.split() for l in Path('/proc/self/mountinfo').read_text().splitlines() if l.split()[4]=='/mnt/cgroup-b')
assert line[3]=='/',line
child=subprocess.check_output(['cat','/proc/self/cgroup'],text=True)
assert ':name=gvm_probe:/\n' in child,child
Path('/mnt/cgroup-a/sibling/cgroup.procs').write_text(str(os.getpid()))
assert entry('self')=='/../sibling'
assert not Path('/mnt/cgroup-b/sibling').exists()
check(libc.setns(original,0x02000000))
assert entry('self')=='/sibling'
os.close(original)
print('CGROUP_V1_NAMED_INHERIT_NAMESPACE_MOUNT_SETNS_PASS')
PY
