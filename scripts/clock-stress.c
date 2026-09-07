#define _GNU_SOURCE
#include <errno.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static atomic_uint failures, signals_seen;
static atomic_int finished;
static atomic_uint workers_done;
static pthread_barrier_t barrier;
static pthread_t workers[16];
static unsigned iterations=20000;

static void handler(int sig) {
    int saved=errno;
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t) != 0) atomic_fetch_add(&failures, 1);
    atomic_fetch_add(&signals_seen, 1);
    errno=saved;
}
static void *worker(void *arg) {
    unsigned index=(uintptr_t)arg;
    pthread_barrier_wait(&barrier);
    for (unsigned n=0;n<iterations;n++) {
        struct { uint64_t left; struct timespec t; uint64_t right; } b={.left=0xabcdef1234567890ULL,.right=0x1234567890abcdefULL};
        int result=(n&1) ? clock_gettime(CLOCK_MONOTONIC,&b.t) : syscall(SYS_clock_gettime,CLOCK_MONOTONIC,&b.t);
        if (result || b.left!=0xabcdef1234567890ULL || b.right!=0x1234567890abcdefULL || b.t.tv_nsec<0 || b.t.tv_nsec>=1000000000) {
            unsigned old=atomic_fetch_add(&failures,1);
            if (old<10) fprintf(stderr,"CLOCK_FAILURE thread=%u iteration=%u raw=%u result=%d errno=%d ptr=%p nsec=%ld\n", index,n,!(n&1),result,errno,(void*)&b.t,b.t.tv_nsec);
        }
        if (!(n%100)) {
            char *map=mmap(NULL,4096,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
            if (map==MAP_FAILED) { atomic_fetch_add(&failures,1); break; }
            map[0]=(char)index;
            if (munmap(map,4096)) atomic_fetch_add(&failures,1);
        }
    }
    atomic_fetch_add(&workers_done,1);
    return NULL;
}
static void *sender(void *unused) {
    while (!atomic_load(&finished)) {
        for (unsigned i=0;i<16;i++) pthread_kill(workers[i],SIGUSR1);
        struct timespec delay={.tv_nsec=1000000};
        nanosleep(&delay,NULL);
    }
    return NULL;
}
int main(int argc,char **argv) {
    if (argc>1) iterations=strtoul(argv[1],NULL,10);
    struct sigaction sa={.sa_handler=handler,.sa_flags=SA_RESTART};
    sigemptyset(&sa.sa_mask);
    if (sigaction(SIGUSR1,&sa,NULL) || pthread_barrier_init(&barrier,NULL,17)) return 2;
    for (uintptr_t i=0;i<16;i++) if (pthread_create(&workers[i],NULL,worker,(void*)i)) return 2;
    pthread_t signaler;
    if (pthread_create(&signaler,NULL,sender,NULL)) return 2;
    pthread_barrier_wait(&barrier);
    while (atomic_load(&workers_done)<16) {
        struct timespec delay={.tv_nsec=1000000};
        nanosleep(&delay,NULL);
    }
    atomic_store(&finished,1);
    pthread_join(signaler,NULL);
    for (unsigned i=0;i<16;i++) pthread_join(workers[i],NULL);
    printf("iterations_per_thread=%u threads=16 failures=%u signals=%u\n",iterations,atomic_load(&failures),atomic_load(&signals_seen));
    return atomic_load(&failures) ? 1 : 0;
}
