#define _GNU_SOURCE
#include <X11/Xlib.h>
#include <X11/Xutil.h>
#include <dlfcn.h>
#include <string.h>

/* VirtualGL assigns rendering attributes in XGetVisualInfo order. Ensure the
 * root visual participates even when an X server lists hundreds of visuals. */
XVisualInfo *XGetVisualInfo(Display *display, long mask, XVisualInfo *template,
                          int *count)
{
    XVisualInfo *(*next)(Display *, long, XVisualInfo *, int *) =
        dlsym(RTLD_NEXT, "XGetVisualInfo");
    if (!next) {
        if (count) *count = 0;
        return NULL;
    }
    XVisualInfo *visuals = next(display, mask, template, count);
    if (!visuals || !count || *count < 2 || mask != VisualScreenMask)
        return visuals;
    VisualID root = XVisualIDFromVisual(DefaultVisual(display, template->screen));
    for (int i = 1; i < *count; i++) {
        if (visuals[i].visualid != root) continue;
        XVisualInfo first = visuals[i];
        memmove(visuals + 1, visuals, i * sizeof(*visuals));
        visuals[0] = first;
        break;
    }
    return visuals;
}
