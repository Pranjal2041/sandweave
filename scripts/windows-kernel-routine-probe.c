/* Execute bounded routines from the pinned Microsoft NT kernel in user mode.
 * The generated blob contains original and segment-rewritten leaf routines.
 * This does not initialize or boot Windows. No privileged API or emulator.
 */
#define _GNU_SOURCE
#include <asm/prctl.h>
#include <cpuid.h>
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
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

#define CODE_BASE UINT64_C(0x50000000)
#define GUEST_BASE UINT64_C(0xffff800000100000)
#define NODE_COUNT 63
#define OPERATIONS 200000
typedef uint64_t (__attribute__((ms_abi)) *nt_fn)(uint64_t, uint64_t, uint64_t);
struct node { uint64_t parent, left, right; };
static void fail(const char *what) { perror(what); exit(1); }
static void require(int ok, const char *what) {
    if (!ok) { fprintf(stderr, "validation failed: %s\n", what); exit(2); }
}
static uint64_t now_ns(void) {
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC, &t)) fail("clock_gettime");
    return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static uint32_t next(uint32_t *state) {
    uint32_t x = *state;
    x ^= x << 13; x ^= x >> 17; x ^= x << 5;
    return *state = x;
}
static nt_fn function(void *code, int routine, int shifted) {
    return (nt_fn)((char *)code + (1 + routine * 2 + shifted) * 4096);
}
static uint64_t node_address(uint64_t base, int index) {
    return base + index * sizeof(struct node);
}
static uint64_t build_tree(struct node *nodes, uint64_t base, int lo, int hi, uint64_t parent) {
    if (lo >= hi) return 0;
    int mid = (lo + hi) / 2;
    uint64_t self = node_address(base, mid);
    nodes[mid].parent = parent ? parent : self;
    nodes[mid].left = build_tree(nodes, base, lo, mid, self);
    nodes[mid].right = build_tree(nodes, base, mid + 1, hi, self);
    return self;
}
static int index_of(uint64_t pointer, uint64_t base) {
    require(pointer >= base && pointer < base + NODE_COUNT * sizeof(struct node), "tree pointer range");
    require((pointer - base) % sizeof(struct node) == 0, "tree pointer alignment");
    return (pointer - base) / sizeof(struct node);
}
static uint64_t normalized(uint64_t pointer, uint64_t base) {
    return pointer ? (uint64_t)index_of(pointer, base) + 1 : 0;
}
static void same_tree(struct node *a, uint64_t ab, struct node *b, uint64_t bb) {
    for (int i = 0; i < NODE_COUNT; ++i) {
        require(normalized(a[i].parent, ab) == normalized(b[i].parent, bb), "parent equivalence");
        require(normalized(a[i].left, ab) == normalized(b[i].left, bb), "left-child equivalence");
        require(normalized(a[i].right, ab) == normalized(b[i].right, bb), "right-child equivalence");
    }
}
static void inorder(struct node *nodes, uint64_t base, uint64_t pointer,
                    uint64_t parent, uint64_t *seen, int *expected) {
    if (!pointer) return;
    int i = index_of(pointer, base);
    require(!(*seen & (UINT64_C(1) << i)), "acyclic tree");
    *seen |= UINT64_C(1) << i;
    require(nodes[i].parent == parent, "parent links");
    inorder(nodes, base, nodes[i].left, pointer, seen, expected);
    require(i == (*expected)++, "binary search ordering");
    inorder(nodes, base, nodes[i].right, pointer, seen, expected);
}
static void check_tree(struct node *nodes, uint64_t base, uint64_t root) {
    uint64_t seen = 0;
    int expected = 0;
    inorder(nodes, base, root, root, &seen, &expected);
    require(expected == NODE_COUNT && seen == (UINT64_C(1) << NODE_COUNT) - 1, "all nodes retained");
}
static void bitmap_test(void *code, unsigned char *data, uint64_t base, int shifted) {
    unsigned char expected[256] = {0};
    memset(data, 0, 4096);
    function(code, 0, shifted)(base, base + 256, 2048);
    uint32_t count;
    uint64_t pointer;
    memcpy(&count, data, 4); memcpy(&pointer, data + 8, 8);
    require(count == 2048 && pointer == base + 256, "bitmap descriptor preserves original pointer");
    uint32_t state = 0xabcde123;
    for (int i = 0; i < 10000; ++i) {
        unsigned bit = next(&state) % 2048;
        int set = next(&state) & 1;
        function(code, set ? 1 : 2, shifted)(base, bit, 0);
        if (set) expected[bit / 8] |= 1U << (bit % 8);
        else expected[bit / 8] &= ~(1U << (bit % 8));
        uint64_t result = function(code, 3, shifted)(base, bit, 0);
        require((result & 255) == (unsigned)set, "RtlTestBit result");
        if (i % 31 == 0) require(!memcmp(expected, data + 256, 256), "bitmap full reference");
    }
    require(!memcmp(expected, data + 256, 256), "bitmap final reference");
}
int main(int argc, char **argv) {
    if (argc != 3 || (strcmp(argv[2], "low") && strcmp(argv[2], "wide"))) {
        fprintf(stderr, "usage: %s kernel-leaves.bin low|wide\n", argv[0]); return 2;
    }
    struct rlimit core = {0, 0};
    if (setrlimit(RLIMIT_CORE, &core)) fail("setrlimit");
    alarm(60);
    uint64_t bias = !strcmp(argv[2], "wide") ? UINT64_C(0x1000000000000) : UINT64_C(0x800000000000);
    int fd = open(argv[1], O_RDONLY);
    if (fd < 0) fail("open code blob");
    struct stat st;
    if (fstat(fd, &st)) fail("fstat");
    require(st.st_size == 11 * 4096, "code blob size");
    void *code = mmap((void *)CODE_BASE, st.st_size, PROT_READ | PROT_EXEC,
                      MAP_PRIVATE | MAP_FIXED_NOREPLACE, fd, 0);
    if (code == MAP_FAILED) fail("map code");
    require((uint64_t)code == CODE_BASE && ((uint64_t *)code)[0] == UINT64_C(0x314b50544e)
            && ((uint64_t *)code)[1] == CODE_BASE, "code blob header and placement");
    close(fd);
    unsigned char *shifted = mmap((void *)(GUEST_BASE + bias), 4096, PROT_READ | PROT_WRITE,
                                 MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    if (shifted == MAP_FAILED) fail("map shifted data");
    require((uint64_t)shifted == GUEST_BASE + bias, "shifted placement");
    unsigned char *native = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (native == MAP_FAILED) fail("map native data");
    unsigned long saved;
    if (syscall(SYS_arch_prctl, ARCH_GET_GS, &saved)) fail("get GS");
    int gs_errno = 0;
    if (syscall(SYS_arch_prctl, ARCH_SET_GS, bias)) {
        gs_errno = errno;
        unsigned a, b, c, d;
        require(__get_cpuid_count(7, 0, &a, &b, &c, &d) && (b & 1), "FSGSBASE support");
        __asm__ volatile ("wrgsbase %0" : : "r" (bias) : "memory");
    }
    bitmap_test(code, native, (uint64_t)native, 0);
    bitmap_test(code, shifted, GUEST_BASE, 1);
    struct node *n = (struct node *)native, *s = (struct node *)shifted;
    build_tree(n, (uint64_t)n, 0, NODE_COUNT, 0);
    build_tree(s, GUEST_BASE, 0, NODE_COUNT, 0);
    uint32_t state = 0xabcdef01;
    for (int i = 0; i < 10000; ++i) {
        int pivot = next(&state) % NODE_COUNT;
        uint64_t nr = function(code, 4, 0)(node_address((uint64_t)n, pivot), 0, 0);
        uint64_t sr = function(code, 4, 1)(node_address(GUEST_BASE, pivot), 0, 0);
        require(nr == node_address((uint64_t)n, pivot) && sr == node_address(GUEST_BASE, pivot), "returned root pointer");
        same_tree(n, (uint64_t)n, s, GUEST_BASE);
        check_tree(s, GUEST_BASE, sr);
    }
    uint64_t native_ns[7], shifted_ns[7];
    for (int round = 0; round < 7; ++round) {
        for (int j = 0; j < 2; ++j) {
            int rewrite = (round + j) & 1;
            struct node *nodes = rewrite ? s : n;
            uint64_t base = rewrite ? GUEST_BASE : (uint64_t)n;
            build_tree(nodes, base, 0, NODE_COUNT, 0);
            nt_fn fn = function(code, 4, rewrite);
            state = 0xabcdef01;
            uint64_t root = 0, start = now_ns();
            for (int op = 0; op < OPERATIONS; ++op)
                root = fn(node_address(base, next(&state) % NODE_COUNT), 0, 0);
            uint64_t elapsed = now_ns() - start;
            check_tree(nodes, base, root);
            if (rewrite) shifted_ns[round] = elapsed; else native_ns[round] = elapsed;
        }
        same_tree(n, (uint64_t)n, s, GUEST_BASE);
    }
    if (syscall(SYS_arch_prctl, ARCH_SET_GS, saved)) fail("restore GS");
    printf("{\"case\":\"microsoft-nt-kernel-leaves\",\"bias_mode\":\"%s\",\"correct\":true,"
           "\"gs_prctl_errno\":%d,\"kernel_booted\":false,\"bitmap_operations\":10000,"
           "\"splay_differential_operations\":10000,\"splay_timed_operations\":%d,\"samples\":[",
           argv[2], gs_errno, OPERATIONS);
    for (int i = 0; i < 7; ++i)
        printf("%s{\"native_ns\":%" PRIu64 ",\"shifted_ns\":%" PRIu64 "}",
               i ? "," : "", native_ns[i], shifted_ns[i]);
    puts("]}");
    munmap(native, 4096); munmap(shifted, 4096); munmap(code, st.st_size);
    return 0;
}
