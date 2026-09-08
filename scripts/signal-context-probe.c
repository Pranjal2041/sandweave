#define _GNU_SOURCE
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <ucontext.h>
#include <unistd.h>

/* Compare the Linux x86 fault ABI using the same binary outside/inside Sentry. */
struct fault {
    int signo, code;
    uintptr_t address, trap, error, cr2;
};
static int output_fd;

static void caught(int signo, siginfo_t *info, void *context) {
    ucontext_t *uc = context;
    struct fault record = {signo, info->si_code, (uintptr_t)info->si_addr,
        uc->uc_mcontext.gregs[REG_TRAPNO], uc->uc_mcontext.gregs[REG_ERR],
        uc->uc_mcontext.gregs[REG_CR2]};
    _exit(write(output_fd, &record, sizeof(record)) == sizeof(record) ? 0 : 2);
}

static void trigger(int test) {
    volatile unsigned int *page = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
                                      MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (page == MAP_FAILED) _exit(3);
    *page = 0xc3; /* ret, for the non-executable-page test. */
    switch (test) {
    case 0: __asm__ volatile("mov (%%rax), %%eax" : : "a"(0x18) : "memory"); break;
    case 1: __asm__ volatile("lock incl 0x18(%%rcx)" : : "c"(0) : "memory"); break;
    case 2:
        if (mprotect((void *)page, 4096, PROT_NONE)) _exit(3);
        __asm__ volatile("mov (%0), %%eax" : : "r"(page) : "rax", "memory"); break;
    case 3:
        if (mprotect((void *)page, 4096, PROT_READ)) _exit(3);
        *page = 1; break;
    case 4: ((void (*)(void))page)(); break;
    case 5: __asm__ volatile("ud2"); break;
    case 6: __asm__ volatile("hlt"); break;
    case 7: __asm__ volatile("int3"); break;
    }
    _exit(4); /* Every case must reach the signal handler. */
}

int main(void) {
    const char *names[] = {"unmapped-read", "unmapped-atomic-write", "protnone-read",
        "readonly-write", "noexec-call", "ud2", "hlt", "int3"};
    for (int test = 0; test < 8; ++test) {
        int pipefd[2], status;
        if (pipe(pipefd)) return 1;
        pid_t child = fork();
        if (child < 0) return 1;
        if (!child) {
            close(pipefd[0]);
            output_fd = pipefd[1];
            struct sigaction action = {.sa_sigaction = caught, .sa_flags = SA_SIGINFO};
            sigemptyset(&action.sa_mask);
            int signals[] = {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGTRAP};
            for (int i = 0; i < 5; ++i)
                if (sigaction(signals[i], &action, NULL)) _exit(3);
            trigger(test);
        }
        close(pipefd[1]);
        struct fault record;
        ssize_t count = read(pipefd[0], &record, sizeof(record));
        close(pipefd[0]);
        if (waitpid(child, &status, 0) != child || count != sizeof(record) ||
            !WIFEXITED(status) || WEXITSTATUS(status)) return 1;
        printf("{\"case\":\"%s\",\"signal\":%d,\"code\":%d,\"address\":\"%#lx\","
               "\"trap\":%lu,\"error\":%lu,\"cr2\":\"%#lx\"}\n",
               names[test], record.signo, record.code, record.address,
               record.trap, record.error, record.cr2);
    }
    return 0;
}
