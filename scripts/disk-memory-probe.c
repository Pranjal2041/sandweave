/* Native file-backed memory benchmark. No global cache drops or host changes.
 * Build: gcc -O3 -march=native -Wall -Wextra -Werror disk-memory-probe.c -o probe
 * Run through profile-disk-memory.py for bounded Slurm memory and telemetry.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define MIB (1024ULL * 1024)
#define PAGE 4096ULL
static volatile uint64_t sink;
static uint64_t *mapping;
static size_t length;
static int backing_fd;

static void fail(const char *what) { perror(what); exit(1); }
static void check(int result, const char *what) { if (result < 0) fail(what); }
static uint64_t mix(uint64_t v) {
    v = (v ^ (v >> 30)) * UINT64_C(0xbf58476d1ce4e5b9);
    v = (v ^ (v >> 27)) * UINT64_C(0x94d049bb133111eb);
    return v ^ (v >> 31);
}
static uint64_t nanos(void) {
    struct timespec t;
    check(clock_gettime(CLOCK_MONOTONIC, &t), "clock_gettime");
    return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static uint64_t io_bytes(const char *name) {
    FILE *f = fopen("/proc/self/io", "r");
    if (!f) fail("/proc/self/io");
    char key[80]; uint64_t value, found = 0;
    while (fscanf(f, "%79s %" SCNu64, key, &value) == 2)
        if (!strcmp(key, name)) found = value;
    fclose(f);
    return found;
}
static uint64_t resident(void) {
    size_t n = length / PAGE;
    unsigned char *v = malloc(n);
    if (!v) fail("mincore buffer");
    check(mincore(mapping, length, v), "mincore");
    uint64_t pages = 0;
    for (size_t i = 0; i < n; i++) pages += v[i] & 1;
    free(v);
    return pages * PAGE;
}
static uint64_t scan_read(uint64_t *p, size_t bytes) {
    uint64_t sum = 0;
    for (size_t i = 0; i < bytes / sizeof(*p); i++) sum += p[i];
    sink = sum;
    return sum;
}
static uint64_t prefault(size_t bytes) {
    uint64_t sum = 0;
    for (size_t i = 0; i < bytes / sizeof(*mapping); i += PAGE / sizeof(*mapping))
        sum += mapping[i];
    sink = sum;
    return sum;
}
static int compare_u64(const void *a, const void *b) {
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return (x > y) - (x < y);
}
static void phase(const char *name, unsigned repeat, uint64_t operations,
                  size_t hot_bytes, int kind) {
    /* kind 0 scans every byte; 1 dependent reads; 2 99%-hot dependent reads;
     * 3 dependent reads and dirty writes. Latencies include dependency math.
     * Writes change a separate word so all cases use identical read addresses.
     */
    uint64_t *latencies = kind ? malloc(operations * sizeof(uint64_t)) : NULL;
    if (kind && !latencies) fail("latencies");
    uint64_t cached_start = resident();
    printf("{\"event\":\"start\",\"phase\":\"%s\",\"repeat\":%u}\n", name, repeat);
    fflush(stdout);
    struct rusage before, after;
    check(getrusage(RUSAGE_SELF, &before), "getrusage");
    uint64_t rb = io_bytes("read_bytes:"), wb = io_bytes("write_bytes:");
    uint64_t start = nanos(), checksum = 0;
    if (!kind) {
        checksum = scan_read(mapping, length);
    } else {
        size_t pages = length / PAGE, hot_pages = hot_bytes / PAGE;
        uint64_t state = 42 + (uint64_t)repeat * 1000003 + (uint64_t)kind * 7000001, previous = 0;
        for (uint64_t i = 0; i < operations; i++) {
            uint64_t t = nanos();
            state = mix(state + previous + i);
            size_t page = state % pages;
            if (kind == 2)
                page = i % 100 ? state % hot_pages : hot_pages + state % (pages - hot_pages);
            previous = *(volatile uint64_t *)(mapping + page * PAGE / 8);
            if (previous != mix(page + 1)) {
                fprintf(stderr, "corrupted page %zu\n", page); exit(2);
            }
            if (kind == 3) mapping[page * PAGE / 8 + 1] ^= state;
            checksum += previous;
            latencies[i] = nanos() - t;
        }
        sink = checksum;
    }
    uint64_t elapsed = nanos() - start;
    uint64_t read_bytes = io_bytes("read_bytes:") - rb;
    uint64_t write_bytes = io_bytes("write_bytes:") - wb;
    check(getrusage(RUSAGE_SELF, &after), "getrusage");
    uint64_t flush_ns = 0;
    if (kind == 3 && backing_fd >= 0) {
        uint64_t t = nanos();
        check(msync(mapping, length, MS_SYNC), "msync dirty pages");
        check(fdatasync(backing_fd), "fdatasync dirty pages");
        flush_ns = nanos() - t;
        write_bytes = io_bytes("write_bytes:") - wb;
    }
    uint64_t p50 = 0, p95 = 0, p99 = 0;
    if (kind) {
        qsort(latencies, operations, sizeof(*latencies), compare_u64);
        p50 = latencies[operations / 2];
        p95 = latencies[operations * 95 / 100];
        p99 = latencies[operations * 99 / 100];
    }
    printf("{\"event\":\"result\",\"phase\":\"%s\",\"repeat\":%u,"
           "\"seconds\":%.9f,\"operations\":%" PRIu64 ",\"bytes_scanned\":%zu,"
           "\"ns_per_operation\":%.3f,\"latency_p50_ns\":%" PRIu64 ","
           "\"latency_p95_ns\":%" PRIu64 ",\"latency_p99_ns\":%" PRIu64 ","
           "\"read_bytes\":%" PRIu64 ",\"write_bytes\":%" PRIu64 ","
           "\"major_faults\":%ld,\"minor_faults\":%ld,"
           "\"cached_start_bytes\":%" PRIu64 ",\"cached_end_bytes\":%" PRIu64 ","
           "\"flush_seconds\":%.9f,\"checksum\":%" PRIu64 "}\n",
           name, repeat, elapsed / 1e9, operations, kind ? 0 : length,
           operations ? (double)elapsed / operations : 0, p50, p95, p99,
           read_bytes, write_bytes, after.ru_majflt - before.ru_majflt,
           after.ru_minflt - before.ru_minflt, cached_start, resident(),
           flush_ns / 1e9, checksum);
    fflush(stdout);
    free(latencies);
}
int main(int argc, char **argv) {
    if (argc != 6 && argc != 7) {
        fprintf(stderr, "usage: probe NEW_FILE SIZE_MIB HOT_MIB OPERATIONS REPEATS [--anonymous]\n");
        return 2;
    }
    int anonymous = argc == 7 && !strcmp(argv[6], "--anonymous");
    if (argc == 7 && !anonymous) { fprintf(stderr, "unknown option\n"); return 2; }
    length = strtoull(argv[2], NULL, 10) * MIB;
    size_t hot = strtoull(argv[3], NULL, 10) * MIB;
    uint64_t operations = strtoull(argv[4], NULL, 10);
    unsigned repeats = strtoul(argv[5], NULL, 10);
    if (sysconf(_SC_PAGESIZE) != PAGE || !length || !hot || hot >= length ||
        operations < 100 || !repeats) { fprintf(stderr, "invalid sizes\n"); return 2; }
    backing_fd = anonymous ? -1 : open(argv[1], O_RDWR | O_CREAT | O_EXCL, 0600);
    if (!anonymous) check(backing_fd, "create private backing file");
    if (anonymous) {
        mapping = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        if (mapping == MAP_FAILED) fail("mmap anonymous memory");
        check(madvise(mapping, length, MADV_NOHUGEPAGE), "disable anonymous huge pages");
    }
    size_t chunk = MIB;
    uint64_t *buffer = malloc(chunk);
    if (!buffer) fail("prepare buffer");
    for (size_t i = 0; i < chunk / 8; i++) buffer[i] = mix(i + 100);
    uint64_t start = nanos();
    puts("{\"event\":\"start\",\"phase\":\"prepare\"}"); fflush(stdout);
    for (size_t off = 0; off < length; off += chunk) {
        for (size_t p = 0; p < chunk / PAGE; p++)
            buffer[p * PAGE / 8] = mix(off / PAGE + p + 1);
        if (anonymous) { memcpy((char *)mapping + off, buffer, chunk); continue; }
        size_t done = 0;
        while (done < chunk) {
            ssize_t n = pwrite(backing_fd, (char *)buffer + done, chunk - done, off + done);
            if (n < 0 && errno == EINTR) continue;
            if (n <= 0) fail("populate backing file");
            done += n;
        }
    }
    free(buffer);
    struct stat st = {0};
    if (!anonymous) {
        check(fdatasync(backing_fd), "sync preparation");
        check(fstat(backing_fd, &st), "stat backing file");
        if ((uint64_t)st.st_blocks * 512 < length) { fprintf(stderr, "sparse file\n"); return 2; }
        int error = posix_fadvise(backing_fd, 0, length, POSIX_FADV_DONTNEED);
        if (error) { errno = error; fail("drop own file cache"); }
        mapping = mmap(NULL, length, PROT_READ | PROT_WRITE, MAP_SHARED, backing_fd, 0);
        if (mapping == MAP_FAILED) fail("mmap backing file");
    }
    check(madvise(mapping, length, MADV_NOHUGEPAGE), "disable huge pages");
    uint64_t cached = resident();
    printf("{\"event\":\"prepared\",\"seconds\":%.6f,\"bytes\":%zu,"
           "\"allocated_bytes\":%" PRIu64 ",\"cached_bytes\":%" PRIu64 "}\n",
           (nanos() - start) / 1e9, length, (uint64_t)st.st_blocks * 512, cached);
    fflush(stdout);
    if (!anonymous && cached > length / 100) { fprintf(stderr, "cold cache check failed\n"); return 2; }
    check(madvise(mapping, length, MADV_SEQUENTIAL), "sequential advice");
    /* MADV_SEQUENTIAL permits dropping already-read pages. Restore NORMAL
     * for the second pass so a RAM baseline can retain the complete file. */
    phase(anonymous ? "first_sequential" : "cold_sequential", 0, 0, hot, 0);
    check(madvise(mapping, length, MADV_NORMAL), "normal advice");
    prefault(length);
    for (unsigned i = 0; i < repeats; i++) phase("sequential", i, 0, hot, 0);
    check(madvise(mapping, length, MADV_RANDOM), "random advice");
    for (unsigned i = 0; i < repeats; i++) phase("random_read", i, operations, hot, 1);
    check(madvise(mapping, hot, MADV_NORMAL), "hot warmup advice");
    for (unsigned i = 0; i < 4; i++) prefault(hot);
    check(madvise(mapping, length, MADV_RANDOM), "random advice");
    for (unsigned i = 0; i < repeats; i++) phase("hot_99_percent", i, operations, hot, 2);
    for (unsigned i = 0; i < repeats; i++) phase("random_write", i, operations, hot, 3);
    check(munmap(mapping, length), "munmap");
    if (!anonymous) check(close(backing_fd), "close backing file");
    puts("{\"event\":\"complete\"}");
    return 0;
}
