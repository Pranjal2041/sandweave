#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
lab_local=$(cat "$lab_root/runs/local-path.txt")
mkdir -p "$lab_local/sources" "$lab_local/build"
if [[ ! -f "$lab_local/sources/linux-7.1.3/Makefile" ]]; then
    tar -xf "$lab_root/downloads/linux-7.1.3.tar.xz" -C "$lab_local/sources"
fi
if ! grep -Fq '#define KSTK_EIP(tsk) PT_REGS_IP(&(tsk)->thread.regs)' "$lab_local/sources/linux-7.1.3/arch/x86/um/asm/processor.h"; then
    patch -d "$lab_local/sources/linux-7.1.3" -p1 < "$lab_root/patches/0001-uml-seccomp-user-instruction-pointer.patch"
fi
if ! grep -Fq 'PT_REGS_SET_SYSCALL_RETURN(regs, PT_REGS_SYSCALL_NR(regs));' "$lab_local/sources/linux-7.1.3/arch/um/include/asm/syscall-generic.h"; then
    patch -d "$lab_local/sources/linux-7.1.3" -p1 < "$lab_root/patches/0002-uml-restore-syscall-number-on-rollback.patch"
fi
cp "$lab_root/notes/uml-smp-7.1.3.config" "$lab_local/build/.config"
make -C "$lab_local/sources/linux-7.1.3" ARCH=um O="$lab_local/build" olddefconfig
make -C "$lab_local/sources/linux-7.1.3" ARCH=um O="$lab_local/build" -j8 linux modules
make -C "$lab_local/sources/linux-7.1.3" ARCH=um O="$lab_local/build" INSTALL_MOD_PATH="$lab_root/tools/uml-smp" modules_install
# Publish a new inode; a running kernel executable must not be overwritten.
cp "$lab_local/build/linux" "$lab_root/tools/uml-smp/bin/linux-net.new"
mv "$lab_root/tools/uml-smp/bin/linux-net.new" "$lab_root/tools/uml-smp/bin/linux-net"
sha256sum "$lab_root/tools/uml-smp/bin/linux-net" | cut -d' ' -f1 > "$lab_root/tools/uml-smp/kernel.sha256"
