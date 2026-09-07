#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <pthread.h>

static double seconds(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) abort();
    return t.tv_sec + t.tv_nsec / 1e9;
}
static uint64_t calculate(uint64_t seed) {
    uint64_t x = seed;
    for (unsigned long i = 0; i < 200000000UL; ++i) {
        x ^= x << 13; x ^= x >> 7; x ^= x << 17;
    }
    return x;
}
struct job { uint64_t seed, result; };
static void *thread_work(void *p) {
    struct job *j = p; j->result = calculate(j->seed); return NULL;
}
int main(void) {
    double start = seconds();
    volatile long sink = 0;
    for (int i=0; i<20000; ++i) sink += syscall(SYS_getpid);
    printf("{\"test\":\"getpid_syscall_20000\",\"seconds\":%.6f}\n",seconds()-start);
    start = seconds();
    uint64_t value = calculate(1);
    printf("{\"test\":\"cpu_single\",\"seconds\":%.6f,\"checksum\":\"%llu\"}\n", seconds()-start,(unsigned long long)value);
    fflush(stdout);
    struct job jobs[4]; pthread_t threads[4];
    start = seconds();
    for(int i=0;i<4;i++) { jobs[i].seed=i+1; if(pthread_create(&threads[i],NULL,thread_work,&jobs[i])) abort(); }
    value=0;
    for(int i=0;i<4;i++) { if(pthread_join(threads[i],NULL)) abort(); value ^= jobs[i].result; }
    printf("{\"test\":\"cpu_4_threads\",\"seconds\":%.6f,\"checksum\":\"%llu\"}\n",seconds()-start,(unsigned long long)value);
    fflush(stdout);
    int pipes[4][2]; pid_t children[4];
    start=seconds();
    for(int i=0;i<4;i++) {
        if(pipe(pipes[i])) abort();
        children[i]=fork();
        if(children[i]<0) abort();
        if(children[i]==0) {
            close(pipes[i][0]); uint64_t v=calculate(i+1);
            if(write(pipes[i][1],&v,sizeof(v))!=sizeof(v)) _exit(1);
            _exit(0);
        }
        close(pipes[i][1]);
    }
    value=0;
    for(int i=0;i<4;i++) {
        uint64_t v; int status;
        if(read(pipes[i][0],&v,sizeof(v))!=sizeof(v)) abort();
        close(pipes[i][0]);
        if(waitpid(children[i],&status,0)<0 || status!=0) abort();
        value^=v;
    }
    printf("{\"test\":\"cpu_4_processes\",\"seconds\":%.6f,\"checksum\":\"%llu\"}\n",seconds()-start,(unsigned long long)value);
    return sink < 0;
}
