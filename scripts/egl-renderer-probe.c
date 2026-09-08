/* Log the real renderer once per EGL thread for the standalone display probe. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <unistd.h>

unsigned int eglMakeCurrent(void *display, void *draw, void *read, void *context)
{
    unsigned int (*real)(void *, void *, void *, void *) = dlsym(RTLD_NEXT, "eglMakeCurrent");
    void *(*get_proc)(const char *) = dlsym(RTLD_NEXT, "eglGetProcAddress");
    unsigned int result = real(display, draw, read, context);
    static _Thread_local int logged;
    if (result && context && !logged && get_proc) {
        const unsigned char *(*get_string)(unsigned int) = get_proc("glGetString");
        if (get_string) {
            const unsigned char *renderer = get_string(0x1f01); /* GL_RENDERER */
            if (renderer) {
                fprintf(stderr, "LAB_EGL_RENDERER pid=%ld renderer=%s version=%s\n",
                        (long)getpid(), renderer, get_string(0x1f02)); /* GL_VERSION */
                logged = 1;
            }
        }
    }
    return result;
}
