#define _GNU_SOURCE
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <sys/xattr.h>
#include <unistd.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <ctype.h>
#include <fcntl.h>

/* Read trusted attributes from our immutable ext4 image when the host VFS
 * hides them from an unprivileged FUSE client. Other attributes use libc.
 * This is only an image-conversion helper, never a guest preload library. */
static ssize_t image_attribute(const char *path, const char *name,
                               void *value, size_t size, int follow) {
    const char *mount = getenv("GVM_EXT4_MOUNT");
    const char *image = getenv("GVM_EXT4_IMAGE");
    if (!mount || !image || strncmp(name, "trusted.", 8) ||
        strncmp(path, mount, strlen(mount)) ||
        (path[strlen(mount)] && path[strlen(mount)] != '/')) {
        errno = ENODATA; return -1;
    }
    for (const char *p = name; *p; p++) {
        if (!isalnum((unsigned char)*p) && *p != '.' && *p != '_' && *p != '-') {
            errno = EINVAL; return -1;
        }
    }
    char names[65536];
    ssize_t names_len = follow ? listxattr(path, names, sizeof(names)) :
                                llistxattr(path, names, sizeof(names));
    if (names_len < 0) return -1;
    int found = 0;
    for (ssize_t off = 0; off < names_len;) {
        size_t len = strnlen(names + off, (size_t)(names_len - off));
        if (len == (size_t)(names_len - off)) { errno = EIO; return -1; }
        if (!strcmp(names + off, name)) found = 1;
        off += (ssize_t)len + 1;
    }
    if (!found) { errno = ENODATA; return -1; }
    struct stat st;
    if ((follow ? stat(path, &st) : lstat(path, &st))) return -1;
    char command[1024];
    int n = snprintf(command, sizeof(command),
        "ea_get -f /proc/self/fd/1 <%llu> %s",
        (unsigned long long)st.st_ino, name);
    if (n < 0 || n >= (int)sizeof(command)) { errno = E2BIG; return -1; }
    int fds[2];
    if (pipe2(fds, O_CLOEXEC)) return -1;
    pid_t pid = fork();
    if (pid < 0) { close(fds[0]); close(fds[1]); return -1; }
    if (!pid) {
        close(fds[0]);
        if (dup2(fds[1], STDOUT_FILENO) < 0) _exit(126);
        close(fds[1]);
        int nullfd = open("/dev/null", O_WRONLY);
        if (nullfd >= 0) { dup2(nullfd, STDERR_FILENO); close(nullfd); }
        execl("/usr/sbin/debugfs", "debugfs", "-R", command, image, (char *)0);
        _exit(127);
    }
    close(fds[1]);
    unsigned char buf[65537];
    size_t used = 0;
    int failed = 0;
    while (used < sizeof(buf)) {
        ssize_t got = read(fds[0], buf + used, sizeof(buf) - used);
        if (got == 0) break;
        if (got < 0) { if (errno == EINTR) continue; failed = errno; break; }
        used += (size_t)got;
    }
    close(fds[0]);
    int status;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return -1;
    }
    if (failed || !WIFEXITED(status) || WEXITSTATUS(status)) {
        errno = failed ? failed : EIO; return -1;
    }
    if (used == sizeof(buf)) { errno = E2BIG; return -1; }
    if (!size) return (ssize_t)used;
    if (size < used) { errno = ERANGE; return -1; }
    memcpy(value, buf, used);
    return (ssize_t)used;
}

ssize_t lgetxattr(const char *path, const char *name, void *value, size_t size) {
    static ssize_t (*real_call)(const char *, const char *, void *, size_t);
    if (!real_call) real_call = dlsym(RTLD_NEXT, "lgetxattr");
    ssize_t ret = real_call(path, name, value, size);
    if (ret < 0 && errno == ENODATA)
        return image_attribute(path, name, value, size, 0);
    return ret;
}

ssize_t getxattr(const char *path, const char *name, void *value, size_t size) {
    static ssize_t (*real_call)(const char *, const char *, void *, size_t);
    if (!real_call) real_call = dlsym(RTLD_NEXT, "getxattr");
    ssize_t ret = real_call(path, name, value, size);
    if (ret < 0 && errno == ENODATA)
        return image_attribute(path, name, value, size, 1);
    return ret;
}
