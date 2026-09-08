/* Architectural probe, NOT a Windows VM. No KVM, TCG, or privileged operations.
 * Build: gcc -O2 -Wall -Wextra -Werror windows-native-address-probe.c -o probe
 * Exercise original high-half pointers through GS-based shifted aliases and
 * measure dependent native loads. Each capability runs in an isolated child.
 */
#define _GNU_SOURCE
#include <asm/prctl.h>
#include <cpuid.h>
#include <errno.h>
#include <inttypes.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <ucontext.h>
#include <unistd.h>

#ifndef PR_SET_SYSCALL_USER_DISPATCH
#define PR_SET_SYSCALL_USER_DISPATCH 59
#endif
#define SHIFT UINT64_C(0x1000000000000)
#define KERNEL_VA UINT64_C(0xffff800000100000)
#define USER_VA UINT64_C(0x40000000)
#define MAGIC_SYSCALL UINT32_C(0x7ffe0001)
#define RESULT UINT64_C(0x12345678)
#define NODES 256
#define LOADS UINT64_C(8000000)
#define CALLS 20000

struct node { uint64_t next, value; };
static volatile sig_atomic_t caught;
static volatile unsigned char selector;

static uint64_t now_ns(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts)) _exit(90);
    return (uint64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static void *map_at(uint64_t va, int fd) {
    void *p = mmap((void *)va, 4096, PROT_READ | PROT_WRITE,
                  MAP_SHARED | MAP_FIXED_NOREPLACE, fd, 0);
    if (p != MAP_FAILED && (uint64_t)p != va) {
        munmap(p, 4096);
        errno = EADDRNOTAVAIL;
        return MAP_FAILED;
    }
    return p;
}

/* Both versions have the same instruction stream except two GS prefixes.
 * The input pointer and every next pointer retain guest virtual addresses. */
__attribute__((noinline))
static uint64_t chase_native(uint64_t ptr, uint64_t n) {
    uint64_t sum;
    __asm__ volatile ("xor %[s], %[s]\n\t"
        "1: add 8(%[p]), %[s]\n\t"
        "mov (%[p]), %[p]\n\t"
        "dec %[n]\n\tjnz 1b"
        : [p] "+r" (ptr), [n] "+r" (n), [s] "=&r" (sum) : : "cc", "memory");
    return sum;
}
__attribute__((noinline))
static uint64_t chase_shifted(uint64_t ptr, uint64_t n) {
    uint64_t sum;
    __asm__ volatile ("xor %[s], %[s]\n\t"
        "1: add %%gs:8(%[p]), %[s]\n\t"
        "mov %%gs:(%[p]), %[p]\n\t"
        "dec %[n]\n\tjnz 1b"
        : [p] "+r" (ptr), [n] "+r" (n), [s] "=&r" (sum) : : "cc", "memory");
    return sum;
}
static void fill(struct node *nodes, uint64_t va) {
    for (unsigned i = 0; i < NODES; ++i) {
        nodes[i].next = va + ((i + 1) % NODES) * sizeof(*nodes);
        nodes[i].value = i + 1;
    }
}
static void address_probe(int wide) {
    const uint64_t bias = wide ? SHIFT : UINT64_C(0x800000000000);
    int fd = memfd_create("windows-address-probe", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, 4096)) _exit(91);
    struct node *k = map_at(KERNEL_VA + bias, fd);
    if (k == MAP_FAILED) {
        printf("{\"case\":\"%s\",\"mapped\":false,\"errno\":%d}\n",
               wide ? "wide-alias" : "low-alias", errno);
        close(fd);
        return;
    }
    struct node *u = NULL;
    if (wide) {
        u = map_at(USER_VA + bias, fd);
        if (u == MAP_FAILED) _exit(92);
    }
    unsigned long saved_gs;
    if (syscall(SYS_arch_prctl, ARCH_GET_GS, &saved_gs)) _exit(93);
    int gs_errno = 0;
    if (syscall(SYS_arch_prctl, ARCH_SET_GS, bias)) {
        gs_errno = errno;
        unsigned a, b, c, d;
        if (!__get_cpuid_count(7, 0, &a, &b, &c, &d) || !(b & 1)) _exit(93);
        /* FSGSBASE is an ordinary userspace instruction. Its native availability
         * is a separate capability from arch_prctl's software address limit. */
        __asm__ volatile ("wrgsbase %0" : : "r" (bias) : "memory");
    }
    fill(k, KERNEL_VA);
    uint64_t expected = LOADS / NODES * NODES * (NODES + 1) / 2;
    uint64_t actual = chase_shifted(KERNEL_VA, LOADS);
    if (actual != expected) _exit(94);
    if (wide) {
        /* Same physical page reached through a guest user pointer; writes must
         * remain coherent with the high-half alias. */
        uint64_t read_value;
        __asm__ volatile ("mov %%gs:8(%1), %0" : "=r" (read_value) : "r" (USER_VA) : "memory");
        if (read_value != 1 || u[17].value != k[17].value) _exit(95);
        __asm__ volatile ("movq $1234, %%gs:8(%0)" : : "r" (USER_VA) : "memory");
        if (k[0].value != 1234) _exit(96);
    }
    uint64_t native_ns[7], shifted_ns[7];
    for (int round = 0; round < 7; ++round) {
        /* Alternate order; each timed run starts after a warm-up traversal. */
        for (int j = 0; j < 2; ++j) {
            int shifted = (round + j) & 1;
            uint64_t p = shifted ? KERNEL_VA : (uint64_t)k;
            fill(k, p);
            if (shifted) chase_shifted(p, NODES * 10); else chase_native(p, NODES * 10);
            uint64_t start = now_ns();
            uint64_t sum = shifted ? chase_shifted(p, LOADS) : chase_native(p, LOADS);
            uint64_t elapsed = now_ns() - start;
            if (sum != expected) _exit(97);
            if (shifted) shifted_ns[round] = elapsed; else native_ns[round] = elapsed;
        }
    }
    if (syscall(SYS_arch_prctl, ARCH_SET_GS, saved_gs)) _exit(98);
    printf("{\"case\":\"%s\",\"mapped\":true,\"correct\":true,\"guest_pointer\":\"0x%" PRIx64 "\","
           "\"bias\":\"0x%" PRIx64 "\",\"gs_prctl_errno\":%d,\"wrgsbase_used\":%s,"
           "\"user_alias_coherent\":%s,\"loads_per_sample\":%" PRIu64 ",\"samples\":[",
           wide ? "wide-alias" : "low-alias", KERNEL_VA, bias, gs_errno, gs_errno ? "true" : "false",
           wide ? "true" : "null", LOADS);
    for (int i = 0; i < 7; ++i)
        printf("%s{\"native_ns\":%" PRIu64 ",\"shifted_ns\":%" PRIu64 "}",
               i ? "," : "", native_ns[i], shifted_ns[i]);
    puts("]}");
    munmap(k, 4096);
    if (wide) munmap(u, 4096);
    close(fd);
}

static void sigsys_handler(int sig, siginfo_t *info, void *context) {
    (void)sig;
    if ((unsigned)info->si_syscall != MAGIC_SYSCALL) _exit(80);
    ucontext_t *uc = context;
    uc->uc_mcontext.gregs[REG_RAX] = RESULT;
    ++caught;
    /* For SUD, permit the handler's rt_sigreturn without another SIGSYS.
     * The caller blocks again immediately before each synthetic guest call. */
    selector = 0;
}
static void syscall_probe(int sud) {
    struct sigaction action = {.sa_sigaction = sigsys_handler, .sa_flags = SA_SIGINFO};
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGSYS, &action, NULL)) _exit(81);
    if (sud) {
        if (prctl(PR_SET_SYSCALL_USER_DISPATCH, 1UL, 0UL, 0UL, &selector)) {
            printf("{\"case\":\"syscall-user-dispatch\",\"supported\":false,\"errno\":%d}\n", errno);
            return;
        }
    } else {
        struct sock_filter filter[] = {
            BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
            BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, MAGIC_SYSCALL, 0, 1),
            BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRAP),
            BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
        };
        struct sock_fprog program = {.len = sizeof(filter) / sizeof(filter[0]), .filter = filter};
        if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) ||
            prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program)) _exit(82);
    }
    uint64_t start = now_ns();
    for (unsigned i = 0; i < CALLS; ++i) {
        if (sud) selector = 1;
        register uint64_t result __asm__("rax") = MAGIC_SYSCALL;
        __asm__ volatile ("syscall" : "+a" (result) : : "rcx", "r11", "memory");
        if (result != RESULT) _exit(83);
    }
    uint64_t elapsed = now_ns() - start;
    if (sud && prctl(PR_SET_SYSCALL_USER_DISPATCH, 0UL, 0UL, 0UL, 0UL)) _exit(84);
    printf("{\"case\":\"%s\",\"supported\":true,\"calls\":%d,\"caught\":%d,\"elapsed_ns\":%" PRIu64 "}\n",
           sud ? "syscall-user-dispatch" : "seccomp-dispatch", CALLS, caught, elapsed);
    if (caught != CALLS) _exit(85);
}
int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    struct rlimit limit = {0, 0};
    if (setrlimit(RLIMIT_CORE, &limit)) return 1;
    int failed = 0;
    for (int i = 0; i < 4; ++i) {
        pid_t pid = fork();
        if (pid < 0) return 2;
        if (!pid) {
            alarm(30);
            if (i < 2) address_probe(i); else syscall_probe(i == 2);
            _exit(0);
        }
        int status;
        if (waitpid(pid, &status, 0) != pid) return 3;
        if (!WIFEXITED(status) || WEXITSTATUS(status)) {
            printf("{\"case_index\":%d,\"child_status\":%d,\"failed\":true}\n", i, status);
            failed = 1;
        }
    }
    return failed;
}
