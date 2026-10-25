/* Set the application GS base, make system calls, then read through %gs.
 * Built without libc so the static binary runs in any guest:
 *   cc -O2 -static -nostdlib -fno-stack-protector -o gs-preserve-test gs-preserve-test.c
 * Prints "gs ok" and exits 0 when the base and the value read through it survive. */
static long sys(long number, long a, long b, long c) {
    long result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(a), "S"(b), "d"(c) : "rcx", "r11", "memory");
    return result;
}

static unsigned long block[4] = {0x5a5a5a5a, 0, 0, 0};

void _start(void) {
    unsigned long value = 0, base = 0;
    if (sys(158, 0x1001, (long)block, 0))  /* arch_prctl(ARCH_SET_GS) */
        sys(60, 2, 0, 0);
    for (int i = 0; i < 1000; i++)
        sys(110, 0, 0, 0);  /* getppid */
    __asm__ volatile("movq %%gs:0, %0" : "=r"(value));
    sys(158, 0x1004, (long)&base, 0);  /* arch_prctl(ARCH_GET_GS) */
    int ok = value == 0x5a5a5a5a && base == (unsigned long)block;
    sys(1, 1, (long)(ok ? "gs ok\n" : "gs bad\n"), ok ? 6 : 7);
    sys(60, ok ? 0 : 1, 0, 0);
}
