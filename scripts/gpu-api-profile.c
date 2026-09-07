#define _GNU_SOURCE
#include <cupti.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include <unistd.h>

/* Diagnostic preload: CUPTI callbacks time driver APIs, including functions
 * obtained via cuGetProcAddress. No GPU counters or activity tracing required.
 * Totals overlap across threads and must not be added as wall-clock latency. */
#define SLOTS 2048
static struct {
    _Atomic(const char *) name;
    _Atomic uint64_t count, elapsed, maximum;
} stats[SLOTS];
static CUpti_SubscriberHandle subscriber;

static uint64_t now(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

static void CUPTIAPI callback(void *unused, CUpti_CallbackDomain domain,
                              CUpti_CallbackId id, const void *data) {
    (void)unused;
    if (domain != CUPTI_CB_DOMAIN_DRIVER_API || id >= SLOTS) return;
    const CUpti_CallbackData *cb = data;
    if (cb->callbackSite == CUPTI_API_ENTER) {
        *cb->correlationData = now();
        return;
    }
    uint64_t elapsed = now() - *cb->correlationData;
    if (cb->functionReturnValue && *(const CUresult *)cb->functionReturnValue &&
        *(const CUresult *)cb->functionReturnValue != CUDA_ERROR_NOT_READY)
        fprintf(stderr, "GPU_API_ERROR %s result=%d\n", cb->functionName,
                *(const CUresult *)cb->functionReturnValue);
    atomic_store(&stats[id].name, cb->functionName);
    atomic_fetch_add(&stats[id].count, 1);
    atomic_fetch_add(&stats[id].elapsed, elapsed);
    uint64_t maximum = atomic_load(&stats[id].maximum);
    while (maximum < elapsed && !atomic_compare_exchange_weak(
               &stats[id].maximum, &maximum, elapsed)) {}
}

static void *report(void *unused) {
    (void)unused;
    for (;;) {
        sleep(5);
        uint64_t timestamp = now();
        for (unsigned id = 0; id < SLOTS; ++id) {
            uint64_t count = atomic_exchange(&stats[id].count, 0);
            uint64_t elapsed = atomic_exchange(&stats[id].elapsed, 0);
            uint64_t maximum = atomic_exchange(&stats[id].maximum, 0);
            const char *name = atomic_load(&stats[id].name);
            if (count && name) fprintf(stderr,
                "GPU_API %.3f %s count=%llu total_ms=%.3f max_ms=%.3f\n",
                timestamp / 1e9, name, (unsigned long long)count,
                elapsed / 1e6, maximum / 1e6);
        }
        fflush(stderr);
    }
    return NULL;
}

static void *enable(void *unused) {
    (void)unused;
    const char *delay = getenv("GPU_API_DELAY");
    sleep(delay ? (unsigned)strtoul(delay, NULL, 10) : 45);
    CUptiResult result = cuptiSubscribe(&subscriber, callback, NULL);
    if (result == CUPTI_SUCCESS)
        result = cuptiEnableDomain(1, subscriber, CUPTI_CB_DOMAIN_DRIVER_API);
    if (result != CUPTI_SUCCESS) {
        const char *message = NULL;
        cuptiGetResultString(result, &message);
        fprintf(stderr, "GPU_API initialization failed: %s\n", message);
        return NULL;
    }
    pthread_t thread;
    int error = pthread_create(&thread, NULL, report, NULL);
    if (error) fprintf(stderr, "GPU_API reporter failed: %d\n", error);
    else pthread_detach(thread);
    fprintf(stderr, "GPU_API initialized pid=%ld\n", (long)getpid());
    return NULL;
}

__attribute__((constructor)) static void preload(void) {
    if (!getenv("GPU_API_PRELOAD")) return;
    pthread_t thread;
    int error = pthread_create(&thread, NULL, enable, NULL);
    if (!error) pthread_detach(thread);
    else fprintf(stderr, "GPU_API initializer thread failed: %d\n", error);
}
