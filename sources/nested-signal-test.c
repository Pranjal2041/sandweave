#define _GNU_SOURCE
#include <asm/prctl.h>
#include <cpuid.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sched.h>
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <ucontext.h>
#include <unistd.h>

/* Exercise the host ABI used by nested systrap, without running a Sentry. */
static unsigned char fpstate[32768] __attribute__((aligned(64)));
static unsigned char *altstack;
enum { ALTSTACK_SIZE = 65536 };
static volatile sig_atomic_t caught;
static int relocate_fp;
static int test_gs = 1;
static int bound_fp_copy;
static unsigned fp_size;
static volatile unsigned signal_fp_size;
static unsigned iterations = 1;

static void handler(int signo, siginfo_t *info, void *context) {
    ucontext_t *uc = context;
    if (signo == SIGSYS && uc->uc_mcontext.gregs[REG_RAX] != info->si_syscall) {
        static const char message[] = "SIGSYS syscall number differs from RAX\n";
        ssize_t written = write(STDERR_FILENO, message, sizeof(message) - 1);
        (void)written;
        _exit(15);
    }
    uint32_t *software = (void *)((char *)uc->uc_mcontext.fpregs + 464);
    signal_fp_size = software[4];
    unsigned long gs;
    if (test_gs && (syscall(SYS_arch_prctl, ARCH_GET_GS, &gs) ||
        syscall(SYS_arch_prctl, ARCH_SET_GS, gs)))
        _exit(20);
    if (relocate_fp) {
        unsigned size = fp_size;
        if (bound_fp_copy && signal_fp_size < size)
            size = signal_fp_size;
        memcpy(fpstate, uc->uc_mcontext.fpregs, size);
        uc->uc_mcontext.fpregs = (void *)fpstate;
    }
    if (signo == SIGSYS)
        uc->uc_mcontext.gregs[REG_RAX] = 12345;
    caught++;
}

static int child(void *unused) {
    (void)unused;
    stack_t stack = {.ss_sp = altstack, .ss_size = ALTSTACK_SIZE};
    if (sigaltstack(&stack, NULL))
        return 10;
    struct sigaction action = {.sa_sigaction = handler,
                               .sa_flags = SA_SIGINFO | SA_ONSTACK};
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGSEGV, &action, NULL) || sigaction(SIGSYS, &action, NULL))
        return 11;
    if (raise(SIGSEGV) || caught != 1)
        return 12;
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getpid, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRAP),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = {.len = sizeof(filter) / sizeof(filter[0]),
                                .filter = filter};
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) ||
        prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program))
        return 13;
    for (unsigned i = 0; i < iterations; i++) {
        if (syscall(SYS_getpid) != 12345 || caught != (sig_atomic_t)(i + 2))
            return 14;
    }
    return 0;
}

int main(int argc, char **argv) {
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--no-gs") == 0) test_gs = 0;
        else if (strcmp(argv[i], "--bound-fp-copy") == 0) bound_fp_copy = 1;
        else if (strcmp(argv[i], "--iterations") == 0 && i + 1 < argc) {
            char *end;
            unsigned long value = strtoul(argv[++i], &end, 10);
            if (*end || value == 0 || value > 1000000)
                return 2;
            iterations = (unsigned)value;
        }
        else return 2;
    }
    altstack = mmap(NULL, ALTSTACK_SIZE + 4096, PROT_READ | PROT_WRITE,
                    MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (altstack == MAP_FAILED ||
        mprotect(altstack + ALTSTACK_SIZE, 4096, PROT_NONE))
        return 3;
    unsigned eax, ebx, ecx, edx;
    __cpuid_count(0xd, 0, eax, ebx, ecx, edx);
    fp_size = ebx;
    if (fp_size < 512 || fp_size > sizeof(fpstate) - 4)
        return 2;
    printf("cpuid_xsave_size=%u xfeatures=%#x\n", fp_size, eax);
    fflush(stdout);
    int failed = 0;
    for (int shared = 0; shared <= 1; shared++) {
        for (relocate_fp = 0; relocate_fp <= 1; relocate_fp++) {
            caught = 0;
            void *stack = mmap(NULL, 1 << 20, PROT_READ | PROT_WRITE,
                               MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
            if (stack == MAP_FAILED)
                return 3;
            int flags = SIGCHLD;
            if (shared)
                flags |= CLONE_VM | CLONE_FILES | CLONE_FS;
            pid_t pid = clone(child, (char *)stack + (1 << 20), flags, NULL);
            if (pid < 0)
                return 4;
            int status;
            if (waitpid(pid, &status, 0) != pid)
                return 5;
            printf("shared_vm=%d relocated_fp=%d status=%#x signal_xsave_size=%u\n",
                   shared, relocate_fp, status, signal_fp_size);
            fflush(stdout);
            failed |= status != 0;
            munmap(stack, 1 << 20);
        }
    }
    return failed;
}
