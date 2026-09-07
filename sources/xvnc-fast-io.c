/* Persistent Xvnc I/O bridge. MIT-SHM 1.2 writes into a donated host file.
 * Wire format is private, little-endian, version 1. No application paint fence
 * is implied by the XTEST server-processing acknowledgement. */
#define _GNU_SOURCE
#include <xcb/xcb.h>
#include <xcb/shm.h>
#include <xcb/xtest.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>

#define MAX_EVENTS 8192
#define PIXEL_OFFSET 64
struct event { uint32_t type, code; int32_t x, y; };
struct frame {
    _Atomic uint64_t sequence;
    uint32_t width, height, stride, reserved;
    uint64_t started_ns, completed_ns, input_sequence;
};
static xcb_connection_t *c;
static xcb_screen_t *screen;
static xcb_shm_seg_t segment;
static struct frame *frame;
static size_t capacity;
static uint64_t input_sequence;
static uint32_t assigned[256];
static xcb_keycode_t first_key, last_key;
static uint8_t columns;
static uint32_t *original_map;

static uint64_t now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec * 1000000000 + t.tv_nsec;
}
static void fatal(const char *message) {
    fprintf(stderr, "fast-io: %s\n", message); exit(1);
}
static void check(xcb_void_cookie_t cookie) {
    xcb_generic_error_t *error = xcb_request_check(c, cookie);
    if (error) { fprintf(stderr, "X11 error %u request %u/%u\n", error->error_code,
                        error->major_code, error->minor_code); free(error); fatal("X11 request failed"); }
}
static int read_all(void *buffer, size_t bytes) {
    size_t received = fread(buffer, 1, bytes, stdin);
    if (received == 0 && feof(stdin)) return 0;
    if (received != bytes) fatal("truncated request");
    return 1;
}
static xcb_get_geometry_reply_t *geometry(void) {
    xcb_get_geometry_reply_t *r = xcb_get_geometry_reply(c, xcb_get_geometry(c, screen->root), NULL);
    if (!r) fatal("cannot read root geometry");
    if ((size_t)r->width * r->height * 4 > capacity - PIXEL_OFFSET)
        fatal("desktop exceeds the 64 MiB screenshot buffer");
    return r;
}
static void capture(void) {
    uint64_t started = now();
    xcb_get_geometry_reply_t *g = geometry();
    atomic_fetch_add_explicit(&frame->sequence, 1, memory_order_seq_cst);
    xcb_generic_error_t *error = NULL;
    xcb_shm_get_image_reply_t *r = xcb_shm_get_image_reply(c,
        xcb_shm_get_image(c, screen->root, 0, 0, g->width, g->height, UINT32_MAX,
                          XCB_IMAGE_FORMAT_Z_PIXMAP, segment, PIXEL_OFFSET), &error);
    if (!r || error || r->size != (uint32_t)g->width * g->height * 4)
        fatal("shared-memory capture failed (possibly a concurrent resize); reconnect");
    frame->width = g->width; frame->height = g->height; frame->stride = g->width * 4;
    frame->started_ns = started; frame->completed_ns = now();
    frame->input_sequence = input_sequence;
    atomic_fetch_add_explicit(&frame->sequence, 1, memory_order_seq_cst);
    free(r); free(g);
}
static void cleanup(void) {
    if (!c) return;
    if (segment) check(xcb_shm_detach_checked(c, segment));
    /* Do not release held input during pause/save. The X server owns it. */
    for (unsigned k = first_key; original_map && k <= last_key; ++k)
        if (assigned[k]) check(xcb_change_keyboard_mapping_checked(c, 1, k, columns,
                                     original_map + (k - first_key) * columns));
    xcb_disconnect(c); c = NULL;
}
int main(void) {
    if (__BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__) fatal("little-endian host required");
    setvbuf(stdout, NULL, _IOLBF, 0);
    c = xcb_connect(NULL, NULL);
    if (xcb_connection_has_error(c)) fatal("cannot connect to the X display");
    const xcb_setup_t *setup = xcb_get_setup(c);
    screen = xcb_setup_roots_iterator(setup).data;
    if (!screen || setup->image_byte_order != XCB_IMAGE_ORDER_LSB_FIRST || screen->root_depth != 24)
        fatal("requires a little-endian 24-bit Xvnc root visual");
    int format_ok = 0, visual_ok = 0;
    for (xcb_format_iterator_t it = xcb_setup_pixmap_formats_iterator(setup); it.rem; xcb_format_next(&it))
        if (it.data->depth == 24 && it.data->bits_per_pixel == 32 && it.data->scanline_pad == 32) format_ok = 1;
    for (xcb_depth_iterator_t d = xcb_screen_allowed_depths_iterator(screen); d.rem; xcb_depth_next(&d))
        for (xcb_visualtype_iterator_t v = xcb_depth_visuals_iterator(d.data); v.rem; xcb_visualtype_next(&v))
            if (v.data->visual_id == screen->root_visual && v.data->red_mask == 0xff0000 &&
                v.data->green_mask == 0xff00 && v.data->blue_mask == 0xff) visual_ok = 1;
    if (!format_ok || !visual_ok) fatal("unsupported pixel format");
    const xcb_query_extension_reply_t *xtest = xcb_get_extension_data(c, &xcb_test_id);
    xcb_shm_query_version_reply_t *shm = xcb_shm_query_version_reply(c, xcb_shm_query_version(c), NULL);
    if (!xtest || !xtest->present || !shm || shm->major_version != 1 || shm->minor_version < 2)
        fatal("XTEST and MIT-SHM 1.2 required");
    free(shm);
    struct stat stat; if (fstat(3, &stat) || stat.st_size < PIXEL_OFFSET) fatal("invalid frame fd");
    capacity = stat.st_size;
    frame = mmap(NULL, capacity, PROT_READ | PROT_WRITE, MAP_SHARED, 3, 0);
    if (frame == MAP_FAILED) fatal("mmap frame fd failed");
    memset(frame, 0, PIXEL_OFFSET);
    segment = xcb_generate_id(c);
    check(xcb_shm_attach_fd_checked(c, segment, dup(3), 0));
    first_key = setup->min_keycode; last_key = setup->max_keycode;
    xcb_get_keyboard_mapping_reply_t *map = xcb_get_keyboard_mapping_reply(c,
        xcb_get_keyboard_mapping(c, first_key, last_key - first_key + 1), NULL);
    if (!map) fatal("cannot read keyboard mapping");
    columns = map->keysyms_per_keycode;
    size_t map_bytes = (last_key - first_key + 1) * columns * sizeof(uint32_t);
    original_map = malloc(map_bytes); if (!original_map) fatal("allocation failed");
    memcpy(original_map, xcb_get_keyboard_mapping_keysyms(map), map_bytes); free(map);
    capture();
    printf("{\"ready\":true,\"version\":1,\"backend\":\"xvnc\"}\n");
    struct event events[MAX_EVENTS];
    uint8_t codes[MAX_EVENTS];
    xcb_void_cookie_t cookies[MAX_EVENTS];
    for (;;) {
        uint32_t header[2];
        if (!read_all(header, sizeof(header))) break;
        unsigned op = header[0], count = header[1];
        if (op == 0 && count == 0) break;
        if (op > 3 || count > MAX_EVENTS || (op == 1 && count)) fatal("invalid request");
        if (count && !read_all(events, count * sizeof(events[0]))) fatal("missing events");
        uint64_t started = now(), action_ns = 0;
        if (count) {
            xcb_get_geometry_reply_t *g = geometry();
            int keyboard = 0;
            for (unsigned i = 0; i < count; ++i) if (events[i].type == 4 || events[i].type == 5) keyboard = 1;
            map = keyboard ? xcb_get_keyboard_mapping_reply(c, xcb_get_keyboard_mapping(c, first_key,
                                                      last_key - first_key + 1), NULL) : NULL;
            if (keyboard && (!map || map->keysyms_per_keycode != columns)) fatal("keyboard layout changed; reconnect");
            uint32_t *symbols = keyboard ? xcb_get_keyboard_mapping_keysyms(map) : original_map;
            uint32_t planned[256]; memcpy(planned, assigned, sizeof(planned));
            const char *invalid = NULL;
            /* Validate the whole batch before emitting input. New Unicode symbols
             * receive distinct unused keycodes, never a key reused mid-batch. */
            for (unsigned i = 0; i < count; ++i) {
                struct event e = events[i]; codes[i] = 0;
                if (e.type == 1) {
                    if (e.x < 0 || e.y < 0 || e.x >= g->width || e.y >= g->height) { invalid = "pointer outside desktop"; break; }
                } else if (e.type == 2 || e.type == 3) {
                    if (e.code < 1 || e.code > 9) { invalid = "invalid pointer button"; break; }
                    codes[i] = e.code;
                } else if (e.type == 4 || e.type == 5) {
                    if (!e.code) { invalid = "empty keysym"; break; }
                    for (unsigned k = first_key; k <= last_key; ++k)
                        if (symbols[(k - first_key) * columns] == e.code) { codes[i] = k; break; }
                    if (!codes[i]) {
                        for (unsigned k = last_key; k >= first_key; --k) {
                            int unused = 1;
                            for (unsigned j = 0; j < columns; ++j)
                                if (symbols[(k - first_key) * columns + j]) unused = 0;
                            if (unused) {
                                codes[i] = k; planned[k] = e.code;
                                symbols[(k - first_key) * columns] = e.code; break;
                            }
                        }
                        if (!codes[i]) { invalid = "no unused keycode for text; close fast I/O and split the text into smaller sessions"; break; }
                    }
                } else { invalid = "unknown input event"; break; }
            }
            if (invalid) {
                free(map); free(g); printf("{\"error\":\"%s\"}\n", invalid); continue;
            }
            for (unsigned k = first_key; k <= last_key; ++k)
                if (planned[k] && planned[k] != assigned[k]) check(xcb_change_keyboard_mapping_checked(c, 1, k, columns,
                                                        symbols + (k - first_key) * columns));
            memcpy(assigned, planned, sizeof(assigned));
            free(map); free(g);
            for (unsigned i = 0; i < count; ++i) {
                struct event e = events[i];
                uint8_t type = e.type == 1 ? XCB_MOTION_NOTIFY : e.type == 2 ? XCB_BUTTON_PRESS :
                               e.type == 3 ? XCB_BUTTON_RELEASE : e.type == 4 ? XCB_KEY_PRESS : XCB_KEY_RELEASE;
                cookies[i] = xcb_test_fake_input_checked(c, type, codes[i], XCB_CURRENT_TIME,
                                                          screen->root, e.x, e.y, 0);
            }
            /* One reply fences all preceding XTEST requests. Checking their
             * cookies afterwards detects asynchronous errors without N fences. */
            xcb_get_input_focus_reply_t *barrier = xcb_get_input_focus_reply(c, xcb_get_input_focus(c), NULL);
            if (!barrier) fatal("input fence failed; delivery outcome unknown");
            free(barrier);
            for (unsigned i = 0; i < count; ++i) check(cookies[i]);
            input_sequence++; action_ns = now() - started;
        }
        if (op == 1 || op == 3) capture();
        printf("{\"input_sequence\":%lu,\"action_ns\":%lu,\"frame_sequence\":%lu,"
               "\"capture_started_ns\":%lu,\"capture_completed_ns\":%lu,\"width\":%u,\"height\":%u}\n",
               input_sequence, action_ns, atomic_load(&frame->sequence), frame->started_ns,
               frame->completed_ns, frame->width, frame->height);
    }
    cleanup(); munmap(frame, capacity); free(original_map); return 0;
}
