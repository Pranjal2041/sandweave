#define _GNU_SOURCE
#include <asm/prctl.h>
#include <errno.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

static uint64_t read_gs(void) {
    uint64_t value;
    __asm__ volatile("movq %%gs:0, %0" : "=r"(value));
    return value;
}
static void fail(const char *what) { perror(what); _exit(1); }
static void signal_handler(int signal) {
    (void)signal;
    uintptr_t base;
    if (syscall(SYS_arch_prctl, ARCH_GET_GS, &base)) fail("signal GET_GS");
    if (!base || read_gs() != *(uint64_t *)base) _exit(2);
}
static void *worker(void *arg) {
    uint64_t value = 0xabcdef0000000000UL + (uintptr_t)arg;
    uintptr_t previous, base;
    if (syscall(SYS_arch_prctl, ARCH_GET_GS, &previous)) fail("GET_GS");
    if (syscall(SYS_arch_prctl, ARCH_SET_GS, &value)) fail("SET_GS");
    for (int i = 0; i < 10000; i++) {
        if (syscall(SYS_arch_prctl, ARCH_GET_GS, &base)) fail("GET_GS loop");
        if (base != (uintptr_t)&value || read_gs() != value) _exit(3);
        syscall(SYS_getpid);
        if (!(i % 100)) {
            if (pthread_kill(pthread_self(), SIGUSR1)) _exit(4);
            sched_yield();
        }
    }
    errno = 0;
    if (syscall(SYS_arch_prctl, ARCH_SET_GS, 1UL << 63) != -1 || errno != EPERM) _exit(5);
    errno = 0;
    if (syscall(SYS_arch_prctl, ARCH_GET_GS, 1) != -1 || errno != EFAULT) _exit(6);
    if (read_gs() != value) _exit(7);
    pid_t child = fork();
    if (child < 0) fail("fork");
    if (!child) _exit(read_gs() == value ? 0 : 8);
    int status;
    if (waitpid(child, &status, 0) != child || status) _exit(9);
    if (syscall(SYS_arch_prctl, ARCH_SET_GS, previous)) fail("restore GS");
    return NULL;
}
int main(void) {
    struct sigaction sa = {.sa_handler = signal_handler};
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGUSR1, &sa, NULL)) fail("sigaction");
    pthread_t threads[4];
    for (uintptr_t i = 0; i < 4; i++)
        if (pthread_create(&threads[i], NULL, worker, (void *)i)) return 10;
    for (int i = 0; i < 4; i++)
        if (pthread_join(threads[i], NULL)) return 11;
    puts("GS_BASE_THREADS_SIGNALS_FORK_ERRORS_PASS");
    return 0;
}
