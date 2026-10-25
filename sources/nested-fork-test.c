#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

/* Stress fork(2) from concurrent threads in concurrent processes. Each fork
 * gets a new address space, as for Python's subprocess with user/group changes.
 * The child checks its copy-on-write snapshot and overwrites it; the parent
 * checks that the child's writes did not reach its own pages. */
enum { BUFFER_WORDS = 64 * 1024 / sizeof(uint64_t) };
static unsigned threads = 4, iterations = 2000, hold_us;

struct result {
    unsigned thread, forks, fork_errors, first_errno, child_failures, parent_corruptions;
};

static uint64_t pattern(unsigned thread, unsigned iteration, unsigned word) {
    return (UINT64_C(0x9e3779b97f4a7c15) * (word + 1)) ^ ((uint64_t)thread << 48) ^ iteration;
}

static void *worker(void *argument) {
    struct result *result = argument;
    unsigned thread = result->thread;
    uint64_t *buffer = mmap(NULL, BUFFER_WORDS * sizeof(uint64_t), PROT_READ | PROT_WRITE,
                            MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (buffer == MAP_FAILED) {
        result->first_errno = errno;
        result->fork_errors++;
        return NULL;
    }
    for (unsigned i = 0; i < iterations; i++) {
        for (unsigned w = 0; w < BUFFER_WORDS; w++)
            buffer[w] = pattern(thread, i, w);
        pid_t pid = fork();
        if (pid < 0) {
            if (!result->fork_errors++)
                result->first_errno = errno;
            continue;
        }
        if (pid == 0) {
            for (unsigned w = 0; w < BUFFER_WORDS; w++)
                if (buffer[w] != pattern(thread, i, w))
                    _exit(3);
            for (unsigned w = 0; w < BUFFER_WORDS; w++)
                buffer[w] = ~pattern(thread, i, w);
            if (hold_us)
                usleep(hold_us);
            _exit(0);
        }
        result->forks++;
        int status;
        if (waitpid(pid, &status, 0) != pid || !WIFEXITED(status) || WEXITSTATUS(status))
            result->child_failures++;
        for (unsigned w = 0; w < BUFFER_WORDS; w++) {
            if (buffer[w] != pattern(thread, i, w)) {
                result->parent_corruptions++;
                break;
            }
        }
    }
    munmap(buffer, BUFFER_WORDS * sizeof(uint64_t));
    return NULL;
}

static int process(unsigned index) {
    pthread_t ids[64];
    struct result results[64];
    memset(results, 0, sizeof(results));
    for (unsigned t = 0; t < threads; t++) {
        results[t].thread = index * 64 + t;
        if (pthread_create(&ids[t], NULL, worker, &results[t]))
            return 10;
    }
    struct result total = {0};
    for (unsigned t = 0; t < threads; t++) {
        pthread_join(ids[t], NULL);
        total.forks += results[t].forks;
        total.child_failures += results[t].child_failures;
        total.parent_corruptions += results[t].parent_corruptions;
        if (results[t].fork_errors && !total.fork_errors)
            total.first_errno = results[t].first_errno;
        total.fork_errors += results[t].fork_errors;
    }
    printf("process=%u forks=%u fork_errors=%u first_errno=%u child_failures=%u parent_corruptions=%u\n",
           index, total.forks, total.fork_errors, total.first_errno, total.child_failures,
           total.parent_corruptions);
    fflush(stdout);
    return total.fork_errors || total.child_failures || total.parent_corruptions;
}

int main(int argc, char **argv) {
    unsigned processes = 8;
    for (int i = 1; i + 1 < argc; i += 2) {
        unsigned long value = strtoul(argv[i + 1], NULL, 10);
        if (strcmp(argv[i], "--processes") == 0 && value && value <= 64) processes = value;
        else if (strcmp(argv[i], "--threads") == 0 && value && value <= 64) threads = value;
        else if (strcmp(argv[i], "--iterations") == 0 && value && value <= 1000000) iterations = value;
        else if (strcmp(argv[i], "--hold-us") == 0 && value <= 10000000) hold_us = value;
        else return 2;
    }
    if (argc % 2 == 0)
        return 2;
    pid_t children[64];
    for (unsigned p = 0; p < processes; p++) {
        children[p] = fork();
        if (children[p] < 0)
            return 4;
        if (children[p] == 0)
            _exit(process(p));
    }
    int failed = 0;
    for (unsigned p = 0; p < processes; p++) {
        int status;
        if (waitpid(children[p], &status, 0) != children[p] || !WIFEXITED(status) || WEXITSTATUS(status))
            failed = 1;
    }
    return failed;
}
