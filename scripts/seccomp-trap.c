#define _GNU_SOURCE
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <stddef.h>
#include <signal.h>
#include <stdio.h>
#include <stdint.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <ucontext.h>
#include <unistd.h>
static void handler(int sig, siginfo_t *info, void *context) {
    ucontext_t *uc = context;
    uintptr_t rip = uc->uc_mcontext.gregs[REG_RIP];
    uintptr_t reported = (uintptr_t)info->si_call_addr;
    long rax = uc->uc_mcontext.gregs[REG_RAX];
    char line[256];
    int n = snprintf(line, sizeof(line), "signal=%d syscall=%d si_call_addr=%#lx context_rip=%#lx context_rax=%ld ip_match=%d nr_match=%d\n", sig, info->si_syscall, (unsigned long)reported, (unsigned long)rip, rax, reported == rip, rax == info->si_syscall);
    write(STDOUT_FILENO, line, n);
    _exit(reported == rip && rax == info->si_syscall ? 0 : 1);
}
int main(void) {
    struct sigaction sa = {.sa_sigaction = handler, .sa_flags = SA_SIGINFO};
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGSYS, &sa, NULL)) return 2;
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_getpid, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_TRAP),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog prog = {.len = sizeof(filter)/sizeof(filter[0]), .filter = filter};
    if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) return 3;
    if (prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &prog)) return 4;
    syscall(SYS_getpid);
    return 5;
}
