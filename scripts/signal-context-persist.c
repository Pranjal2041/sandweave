#define _GNU_SOURCE
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <ucontext.h>
#include <unistd.h>

static volatile int *page;

static void check_signal(int signo, siginfo_t *info, void *context) {
    (void)info;
    ucontext_t *uc = context;
    if (uc->uc_mcontext.gregs[REG_TRAPNO] != 14 ||
        (uc->uc_mcontext.gregs[REG_ERR] & 0x1e) != 6 ||
        (uintptr_t)uc->uc_mcontext.gregs[REG_CR2] != (uintptr_t)page) {
        uint64_t failure[] = {signo, uc->uc_mcontext.gregs[REG_TRAPNO],
            uc->uc_mcontext.gregs[REG_ERR], uc->uc_mcontext.gregs[REG_CR2],
            (uintptr_t)page};
        ssize_t written = write(STDOUT_FILENO, failure, sizeof(failure));
        _exit(written == sizeof(failure) ? 10 : 12);
    }
    if (signo == SIGSEGV && syscall(SYS_mprotect, page, 4096, PROT_READ | PROT_WRITE))
        _exit(11);
}

int main(void) {
    page = mmap(NULL, 4096, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (page == MAP_FAILED) return 2;
    struct sigaction action = {.sa_sigaction = check_signal, .sa_flags = SA_SIGINFO};
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGSEGV, &action, NULL) || sigaction(SIGUSR1, &action, NULL)) return 3;
    *page = 42;
    int ready = open("/tmp/signal-ready", O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (ready < 0) return 4;
    close(ready);
    while (access("/tmp/signal-release", F_OK)) usleep(20000);
    if (raise(SIGUSR1)) return 5;
    pid_t child = fork();
    if (child < 0) return 6;
    if (!child) {
        if (raise(SIGUSR1)) _exit(7);
        _exit(*page == 42 ? 0 : 8);
    }
    int status;
    if (waitpid(child, &status, 0) != child || status) return 9;
    puts("PASS: RAM value and last exception survived; async signal and fork agree");
    return 0;
}
