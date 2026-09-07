#!/bin/bash
set -euo pipefail
lab_root=$(cd "$(dirname "$0")/.." && pwd -P)
lab_local=$(cat "$lab_root/runs/local-path.txt")
source_dir="$lab_local/sources/linux-7.1.3"
build_dir="$lab_local/build-up"
mkdir -p "$lab_local/sources"
if [[ ! -f "$source_dir/Makefile" ]]; then
    tar -xf "$lab_root/downloads/linux-7.1.3.tar.xz" -C "$lab_local/sources"
fi
if ! grep -Fq '#define KSTK_EIP(tsk) PT_REGS_IP(&(tsk)->thread.regs)' "$source_dir/arch/x86/um/asm/processor.h"; then
    patch -d "$source_dir" -p1 < "$lab_root/patches/0001-uml-seccomp-user-instruction-pointer.patch"
fi
if ! grep -Fq 'PT_REGS_SET_SYSCALL_RETURN(regs, PT_REGS_SYSCALL_NR(regs));' "$source_dir/arch/um/include/asm/syscall-generic.h"; then
    patch -d "$source_dir" -p1 < "$lab_root/patches/0002-uml-restore-syscall-number-on-rollback.patch"
fi
mkdir -p "$build_dir" "$lab_root/tools/uml-up/bin"
cp "$lab_root/notes/uml-smp-7.1.3.config" "$build_dir/.config"
"$source_dir/scripts/config" --file "$build_dir/.config" --disable SMP --set-val NR_CPUS 1
make -C "$source_dir" ARCH=um O="$build_dir" olddefconfig
make -C "$source_dir" ARCH=um O="$build_dir" -j8 linux modules
make -C "$source_dir" ARCH=um O="$build_dir" INSTALL_MOD_PATH="$lab_root/tools/uml-up" modules_install
cp "$build_dir/.config" "$lab_root/notes/uml-up-7.1.3.config"
cp "$build_dir/linux" "$lab_root/tools/uml-up/bin/linux.new"
mv "$lab_root/tools/uml-up/bin/linux.new" "$lab_root/tools/uml-up/bin/linux"
sha256sum "$lab_root/tools/uml-up/bin/linux" | cut -d' ' -f1 > "$lab_root/tools/uml-up/kernel.sha256"
