#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

/* Exercise an actual CUDA kernel through the runtime's GPU proxy, without
 * requiring the CUDA toolkit in the test filesystem. */
static void *driver;
#define API(name, signature) \
    int (*name) signature = dlsym(driver, #name); \
    if (!name) { fprintf(stderr, "Missing CUDA API: %s\n", #name); return 2; }
#define CHECK(call) do { \
    int result = (call); \
    if (result) { fprintf(stderr, "%s failed: CUDA error %d\n", #call, result); return 1; } \
} while (0)

int main(void) {
    driver = dlopen("libcuda.so.1", RTLD_NOW);
    if (!driver) { fprintf(stderr, "%s\n", dlerror()); return 2; }
    API(cuInit, (unsigned));
    API(cuDeviceGetCount, (int *));
    API(cuDeviceGetName, (char *, int, int));
    API(cuDevicePrimaryCtxRetain, (void **, int));
    API(cuCtxSetCurrent, (void *));
    API(cuMemAlloc_v2, (uint64_t *, size_t));
    API(cuMemcpyHtoD_v2, (uint64_t, const void *, size_t));
    API(cuMemcpyDtoH_v2, (void *, uint64_t, size_t));
    API(cuModuleLoadData, (void **, const void *));
    API(cuModuleGetFunction, (void **, void *, const char *));
    API(cuLaunchKernel, (void *, unsigned, unsigned, unsigned, unsigned,
                        unsigned, unsigned, unsigned, void *, void **, void **));
    API(cuCtxSynchronize, (void));
    API(cuMemFree_v2, (uint64_t));
    API(cuModuleUnload, (void *));
    API(cuDevicePrimaryCtxRelease_v2, (int));
    CHECK(cuInit(0));
    int count;
    CHECK(cuDeviceGetCount(&count));
    if (count != 1) { fprintf(stderr, "Expected one GPU, got %d\n", count); return 1; }
    char name[256];
    CHECK(cuDeviceGetName(name, sizeof(name), 0));
    printf("CUDA_DEVICE=%s\n", name);
    fflush(stdout);
    void *context, *module, *function;
    CHECK(cuDevicePrimaryCtxRetain(&context, 0));
    CHECK(cuCtxSetCurrent(context));
    uint64_t device;
    CHECK(cuMemAlloc_v2(&device, sizeof(uint32_t)));
    uint32_t value = 41;
    CHECK(cuMemcpyHtoD_v2(device, &value, sizeof(value)));
    const char *ptx =
        ".version 8.0\n.target sm_80\n.address_size 64\n"
        ".visible .entry add_one(.param .u64 address) {\n"
        ".reg .u64 ptr;\n.reg .u32 val;\n"
        "ld.param.u64 ptr, [address];\n"
        "ld.global.u32 val, [ptr];\n"
        "add.u32 val, val, 1;\n"
        "st.global.u32 [ptr], val;\nret;\n}\n";
    CHECK(cuModuleLoadData(&module, ptx));
    CHECK(cuModuleGetFunction(&function, module, "add_one"));
    void *arguments[] = {&device};
    CHECK(cuLaunchKernel(function, 1, 1, 1, 1, 1, 1, 0, NULL, arguments, NULL));
    CHECK(cuCtxSynchronize());
    CHECK(cuMemcpyDtoH_v2(&value, device, sizeof(value)));
    if (value != 42) { fprintf(stderr, "Wrong GPU result: %u\n", value); return 1; }
    CHECK(cuMemFree_v2(device));
    CHECK(cuModuleUnload(module));
    CHECK(cuDevicePrimaryCtxRelease_v2(0));
    puts("CUDA_KERNEL_OK result=42");
    return 0;
}
