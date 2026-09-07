/* Observe successful native MPS server RM-root allocations without modifying them.
 * NVIDIA NVOS21/NVOS64 layouts: open-gpu-kernel-modules 610.43.02, nvos.h.
 * This library is loaded only by our private host controller/server, never guests.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

int ioctl(int fd, unsigned long request, ...) {
    static int (*original)(int, unsigned long, ...);
    if (!original) original = dlsym(RTLD_NEXT, "ioctl");
    va_list args;
    va_start(args, request);
    void *params = va_arg(args, void *);
    va_end(args);
    int result = original(fd, request, params);
    int saved_errno = errno;
    const char *output = getenv("GENERAL_VM_MPS_RM_LOG");
    size_t size = _IOC_SIZE(request);
    if (output && !strcmp(program_invocation_short_name, "nvidia-cuda-mps-server") &&
        result == 0 && _IOC_TYPE(request) == 'F' && _IOC_NR(request) == 0x2b &&
        (size == 32 || size == 48) && params) {
        const uint32_t *words = params;
        uint32_t cls = words[3], status = words[size == 32 ? 7 : 10];
        if (!status && !words[0] && (cls == 0 || cls == 1 || cls == 0x41)) {
            char line[128];
            int len = snprintf(line, sizeof(line), "{\"pid\":%d,\"handle\":%u}\n",
                               getpid(), words[2]);
            int out = open(output, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
            if (out >= 0) {
                if (write(out, line, len) != len) { /* Startup requires a complete record. */ }
                close(out);
            }
        }
    }
    errno = saved_errno;
    return result;
}
