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
#include <poll.h>
#include <errno.h>

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
static uint64_t last_used[256];
static xcb_keycode_t first_key, last_key;
static uint8_t columns;
static uint32_t *original_map;
static xcb_atom_t wm_protocols, wm_ping, wm_state;
static xcb_window_t pending_windows[256];
static unsigned pending_count;
static uint32_t ping_token;
static void fatal(const char *message);

static xcb_atom_t atom(const char *name) {
    xcb_intern_atom_reply_t *r = xcb_intern_atom_reply(c,
        xcb_intern_atom(c, 0, strlen(name), name), NULL);
    if (!r) fatal("cannot intern atom");
    xcb_atom_t result = r->atom; free(r); return result;
}

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
/* Keep the clients that received mapped keys even if focus later moves. */
static xcb_window_t focused_client(xcb_window_t window) {
    for (unsigned depth = 0; window > XCB_INPUT_FOCUS_POINTER_ROOT && window != screen->root && depth < 64; ++depth) {
        xcb_get_property_reply_t *r = xcb_get_property_reply(c,
            xcb_get_property(c, 0, window, wm_state, XCB_GET_PROPERTY_TYPE_ANY, 0, 2), NULL);
        int managed = r && r->type != XCB_ATOM_NONE; free(r);
        if (managed) return window;
        xcb_query_tree_reply_t *tree = xcb_query_tree_reply(c, xcb_query_tree(c, window), NULL);
        if (!tree) return 0;
        window = tree->parent; free(tree);
    }
    return 0;
}
static int remember_client(xcb_window_t window) {
    if (!window) return 0;
    for (unsigned i = 0; i < pending_count; ++i)
        if (pending_windows[i] == window) return 1;
    if (pending_count == 256) return 0;
    pending_windows[pending_count++] = window;
    return 1;
}
/* XTEST's server fence does not drain application queues. Before reusing a
 * keysym, ping the clients that received mapped input: their X event loops
 * must pass our marker before the old mapping can change. Unsupported or
 * stalled clients reject recycling before new input, rather than corrupting
 * text. This is not a paint fence or a fence for arbitrary raw observers. */
static const char *drain_clients(void) {
    unsigned count = pending_count;
    uint32_t windows[256]; memcpy(windows, pending_windows, sizeof(windows));
    uint32_t token = ++ping_token;
    for (unsigned i = 0; i < count; ++i) {
        xcb_client_message_event_t event = {.response_type = XCB_CLIENT_MESSAGE,
            .format = 32, .window = windows[i], .type = wm_protocols};
        event.data.data32[0] = wm_ping; event.data.data32[1] = token;
        event.data.data32[2] = windows[i];
        xcb_generic_error_t *error = xcb_request_check(c,
            xcb_send_event_checked(c, 0, windows[i], XCB_EVENT_MASK_NO_EVENT, (char *)&event));
        if (error) {
            if (error->error_code == XCB_WINDOW) windows[i] = 0;
            else { free(error); return "cannot send Unicode keymap drain marker"; }
            free(error);
        }
    }
    uint64_t deadline = now() + 3000000000ull;
    for (;;) {
        xcb_generic_event_t *event;
        while ((event = xcb_poll_for_event(c))) {
            if ((event->response_type & 127) == XCB_CLIENT_MESSAGE) {
                xcb_client_message_event_t *reply = (xcb_client_message_event_t *)event;
                if (reply->format == 32 && reply->window == screen->root && reply->type == wm_protocols &&
                    reply->data.data32[0] == wm_ping && reply->data.data32[1] == token)
                    for (unsigned i = 0; i < count; ++i)
                        if (windows[i] == reply->data.data32[2]) windows[i] = 0;
            } else if ((event->response_type & 127) == XCB_DESTROY_NOTIFY) {
                for (unsigned i = 0; i < count; ++i)
                    if (windows[i] == ((xcb_destroy_notify_event_t *)event)->window) windows[i] = 0;
            }
            free(event);
        }
        unsigned pending = 0;
        for (unsigned i = 0; i < count; ++i) pending += windows[i] != 0;
        if (!pending) { pending_count = 0; return NULL; }
        if (now() >= deadline) {
            return "cannot safely recycle Unicode keycodes: application did not answer _NET_WM_PING within 3 seconds";
        }
        struct pollfd fd = {.fd = xcb_get_file_descriptor(c), .events = POLLIN};
        if (xcb_connection_has_error(c) || (poll(&fd, 1, 20) < 0 && errno != EINTR)) {
            return "X connection failed while draining Unicode input";
        }
    }
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
    int mapped = 0;
    for (unsigned k = first_key; k <= last_key; ++k) mapped |= assigned[k] != 0;
    const char *error = mapped ? drain_clients() : NULL;
    if (error) fatal(error);
    if (segment) check(xcb_shm_detach_checked(c, segment));
    /* Do not release held input during pause/save. The X server owns it. */
    for (unsigned k = first_key; original_map && k <= last_key; ++k)
        if (assigned[k]) check(xcb_change_keyboard_mapping_checked(c, 1, k, columns,
                                     original_map + (k - first_key) * columns));
    xcb_disconnect(c); c = NULL;
}
static const char *plan_batch(const struct event *events, unsigned count, unsigned width,
                             unsigned height, uint32_t *symbols, const uint8_t *pressed,
                             uint8_t *codes, uint32_t *planned) {
    uint8_t reserved[256] = {0};
    memcpy(planned, assigned, sizeof(assigned));
    /* Reserve the entire batch first, including symbols used after a cache
     * miss. Held keys retain their mapping until their matching release. */
    for (unsigned k = first_key; k <= last_key; ++k) {
        if (pressed && (pressed[k / 8] & (1u << (k % 8)))) reserved[k] = 1;
        for (unsigned i = 0; i < count; ++i)
            if ((events[i].type == 4 || events[i].type == 5) &&
                symbols[(k - first_key) * columns] == events[i].code) reserved[k] = 1;
    }
    for (unsigned i = 0; i < count; ++i) {
        struct event e = events[i]; codes[i] = 0;
        if (e.type == 1) {
            if (e.x < 0 || e.y < 0 || (unsigned)e.x >= width || (unsigned)e.y >= height)
                return "pointer outside desktop";
        } else if (e.type == 2 || e.type == 3) {
            if (e.code < 1 || e.code > 9) return "invalid pointer button";
            codes[i] = e.code;
        } else if (e.type == 4 || e.type == 5) {
            if (!e.code) return "empty keysym";
            for (unsigned k = first_key; k <= last_key; ++k)
                if (symbols[(k - first_key) * columns] == e.code) { codes[i] = k; break; }
            if (!codes[i]) {
                unsigned victim = 0;
                for (unsigned k = last_key; k >= first_key; --k) {
                    if (reserved[k]) continue;
                    int unused = 1;
                    for (unsigned j = 0; j < columns; ++j)
                        if (symbols[(k - first_key) * columns + j]) unused = 0;
                    if (unused) { victim = k; break; }
                    if (assigned[k] && symbols[(k - first_key) * columns] == assigned[k] &&
                        (!victim || last_used[k] < last_used[victim])) victim = k;
                }
                if (!victim) return "one batch exceeds the available Unicode keycodes; held keys are protected";
                codes[i] = victim; planned[victim] = e.code;
                memset(symbols + (victim - first_key) * columns, 0, columns * sizeof(uint32_t));
                symbols[(victim - first_key) * columns] = e.code;
                reserved[victim] = 1;
            }
        } else return "unknown input event";
    }
    return NULL;
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
    wm_protocols = atom("WM_PROTOCOLS"); wm_ping = atom("_NET_WM_PING");
    wm_state = atom("WM_STATE"); ping_token = xcb_generate_id(c);
    uint32_t mask = XCB_EVENT_MASK_SUBSTRUCTURE_NOTIFY;
    check(xcb_change_window_attributes_checked(c, screen->root, XCB_CW_EVENT_MASK, &mask));
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
        if (op > 4 || count > MAX_EVENTS || ((op == 1 || op == 4) && count)) fatal("invalid request");
        if (count && !read_all(events, count * sizeof(events[0]))) fatal("missing events");
        uint64_t started = now(), action_ns = 0;
        if (op == 4) {
            /* A failed detach preflight leaves the channel usable. The caller
             * can release a held key or unstall an app and retry pause/save. */
            xcb_query_keymap_reply_t *held = xcb_query_keymap_reply(c, xcb_query_keymap(c), NULL);
            if (!held) fatal("cannot read held keys before detach");
            const char *invalid = NULL;
            for (unsigned k = first_key; k <= last_key; ++k)
                if (assigned[k] && (held->keys[k / 8] & (1u << (k % 8))))
                    invalid = "release temporary Unicode keys before detaching fast I/O";
            free(held);
            if (!invalid) invalid = drain_clients();
            if (invalid) { printf("{\"error\":\"%s\"}\n", invalid); continue; }
        }
        if (count) {
            xcb_get_geometry_reply_t *g = geometry();
            int keyboard = 0;
            for (unsigned i = 0; i < count; ++i) if (events[i].type == 4 || events[i].type == 5) keyboard = 1;
            map = keyboard ? xcb_get_keyboard_mapping_reply(c, xcb_get_keyboard_mapping(c, first_key,
                                                      last_key - first_key + 1), NULL) : NULL;
            if (keyboard && (!map || map->keysyms_per_keycode != columns)) fatal("keyboard layout changed; reconnect");
            uint32_t *symbols = keyboard ? xcb_get_keyboard_mapping_keysyms(map) : original_map;
            xcb_query_keymap_reply_t *pressed = keyboard ?
                xcb_query_keymap_reply(c, xcb_query_keymap(c), NULL) : NULL;
            if (keyboard && !pressed) fatal("cannot read held keys");
            uint32_t planned[256];
            const char *invalid = plan_batch(events, count, g->width, g->height, symbols,
                                             pressed ? pressed->keys : NULL, codes, planned);
            free(pressed);
            int recycling = 0;
            for (unsigned k = first_key; k <= last_key; ++k)
                if (assigned[k] && planned[k] != assigned[k]) recycling = 1;
            if (!invalid && recycling) invalid = drain_clients();
            int mapped_input = 0;
            xcb_window_t target = 0;
            for (unsigned i = 0; !invalid && i < count; ++i)
                if ((events[i].type == 4 || events[i].type == 5) && planned[codes[i]]) mapped_input = 1;
            if (!invalid && mapped_input) {
                xcb_get_input_focus_reply_t *focus = xcb_get_input_focus_reply(c, xcb_get_input_focus(c), NULL);
                target = focus ? focused_client(focus->focus) : 0;
                free(focus);
                if (!remember_client(target)) invalid = "Unicode input requires a focused managed X11 window and a bounded client history";
            }
            if (invalid) {
                free(map); free(g); printf("{\"error\":\"%s\"}\n", invalid); continue;
            }
            xcb_void_cookie_t mappings[256]; unsigned changed = 0;
            for (unsigned k = first_key; k <= last_key; ++k)
                if (planned[k] && planned[k] != assigned[k])
                    mappings[changed++] = xcb_change_keyboard_mapping_checked(c, 1, k, columns,
                                                        symbols + (k - first_key) * columns);
            for (unsigned i = 0; i < changed; ++i) check(mappings[i]);
            memcpy(assigned, planned, sizeof(assigned));
            /* MappingNotify also has to reach the client before new keycodes
             * are used. Otherwise a busy toolkit can decode using its old map. */
            invalid = changed ? drain_clients() : NULL;
            if (invalid) {
                free(map); free(g); printf("{\"error\":\"%s\"}\n", invalid); continue;
            }
            if (mapped_input && !remember_client(target)) fatal("Unicode client history overflow");
            for (unsigned i = 0; i < count; ++i)
                if ((events[i].type == 4 || events[i].type == 5) && assigned[codes[i]])
                    last_used[codes[i]] = input_sequence + 1;
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
            if (mapped_input) {
                xcb_window_t target = focused_client(barrier->focus);
                if (target && !remember_client(target)) fatal("Unicode client history overflow; delivery outcome unknown");
            }
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
