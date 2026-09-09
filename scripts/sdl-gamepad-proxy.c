/* Userspace SDL2 joystick for disposable Unity/Linux lab processes.
 * Build with build-sdl-gamepad-proxy.py; opt in with SDL_DYNAMIC_API.
 * The player's own SDL implements every function except this joystick API.
 * No evdev/uinput device, game assembly patch, or host privilege is required.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <time.h>
#include <unistd.h>

enum {
#define SDL_DYNAPI_PROC(rc, fn, params, args, ret) SLOT_##fn,
#include "SDL_dynapi_procs.h"
#undef SDL_DYNAPI_PROC
    SLOT_COUNT
};
typedef struct { uint8_t data[16]; } Guid;
typedef struct __attribute__((packed)) {
    char magic[8];
    uint64_t sequence, expires_ns;
    int16_t axes[8];
    uint32_t buttons;
    uint8_t hat, padding[3];
} State;
_Static_assert(sizeof(State) == 48, "gamepad wire ABI");
static const State neutral;
static State state;
static State emitted;
static uint64_t seen_sequence;
static int handle;
static void (*real_pump)(void);
static void (*real_update)(void);
static int (*real_poll)(void *);
static int (*real_push)(void *);
static uint32_t (*real_was_init)(uint32_t);
static int announced;
static const Guid guid = {{3,0,0,0,0x5e,4,0,0,0x8e,2,0,0,0x14,1,0,0}};

static void refresh(void) {
    const char *path = getenv("LAB_GAMEPAD_STATE");
    if (!path) path = "/tmp/lab-gamepad.bin";
    State next;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    ssize_t size = fd < 0 ? -1 : read(fd, &next, sizeof(next));
    if (fd >= 0) close(fd);
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint64_t ns = (uint64_t)now.tv_sec * 1000000000 + now.tv_nsec;
    if (size == sizeof(next) && !memcmp(next.magic, "LABPAD1\0", 8) && next.expires_ns > ns) {
        state = next;
        if (state.sequence != seen_sequence) {
            seen_sequence = state.sequence;
            fprintf(stderr, "lab-gamepad: sequence=%lu buttons=0x%x axes=%d,%d,%d,%d,%d,%d,%d,%d\n",
                    state.sequence, state.buttons, state.axes[0], state.axes[1], state.axes[2],
                    state.axes[3], state.axes[4], state.axes[5], state.axes[6], state.axes[7]);
        }
    } else state = neutral;
}
static int num(void) { return 1; }
static const char *name_index(int i) { return i == 0 ? "Lab Virtual Gamepad" : NULL; }
static void *open_index(int i) {
    if (i != 0) return NULL;
    fprintf(stderr, "lab-gamepad: joystick opened\n");
    return &handle;
}
static const char *name(void *p) { return p == &handle ? name_index(0) : NULL; }
static Guid device_guid(int i) { (void)i; return guid; }
static Guid get_guid(void *p) { (void)p; return guid; }
static int attached(void *p) { return p == &handle; }
static int instance(void *p) { return p == &handle ? 0 : -1; }
static int device_instance(int i) { return i == 0 ? 0 : -1; }
static int axes(void *p) { return p == &handle ? 8 : 0; }
static int zero_handle(void *p) { (void)p; return 0; }
static int hats(void *p) { return p == &handle ? 1 : 0; }
static int buttons(void *p) { return p == &handle ? 15 : 0; }
static void emit(uint32_t type, int index, int value) {
    /* SDL2 SDL_Event is 56 bytes on this x86_64 ABI. Joystick events share
       type, timestamp and instance-id, then an axis/hat/button byte. */
    union { uint64_t align; uint8_t bytes[56]; } event = {0};
    memcpy(event.bytes, &type, 4);
    event.bytes[12] = index;
    if (type == 0x600) {
        int16_t axis_value = value;
        memcpy(event.bytes + 16, &axis_value, 2);
    } else event.bytes[13] = value;
    real_push(&event);
}
static int poll_event(void *event) {
    State before = emitted;
    refresh();
    if (!announced && (real_was_init(0x200) & 0x200)) {
        announced = 1;
        emit(0x605, 0, 0);  /* SDL_JOYDEVICEADDED, device index zero */
        fprintf(stderr, "lab-gamepad: device-added event\n");
    }
    if (announced) {
        for (int i = 0; i < 8; ++i)
            if (before.axes[i] != state.axes[i]) emit(0x600, i, state.axes[i]);
        for (int i = 0; i < 15; ++i)
            if (((before.buttons ^ state.buttons) >> i) & 1)
                emit((state.buttons >> i) & 1 ? 0x603 : 0x604, i, (state.buttons >> i) & 1);
        if (before.hat != state.hat) emit(0x602, 0, state.hat);
        emitted = state;
    }
    return real_poll(event);
}
static void update(void) { refresh(); if (real_update) real_update(); }
static void pump(void) { refresh(); if (real_pump) real_pump(); }
static int16_t axis(void *p, int i) { return p == &handle && i >= 0 && i < 8 ? state.axes[i] : 0; }
static uint8_t hat(void *p, int i) { return p == &handle && i == 0 ? state.hat : 0; }
static uint8_t button(void *p, int i) { return p == &handle && i >= 0 && i < 15 ? (state.buttons >> i) & 1 : 0; }
static int ball(void *p, int i, int *x, int *y) { (void)p; (void)i; if(x)*x=0; if(y)*y=0; return -1; }
static void close_handle(void *p) { (void)p; }
static int initial(void *p, int i, int16_t *v) { if (v) *v=(i >= 0 && i < 8) ? neutral.axes[i] : 0; return p == &handle && i >= 0 && i < 8; }
static int not_controller(int i) { (void)i; return 0; }
static void *from_instance(int i) { return i == 0 ? &handle : NULL; }
static uint16_t vendor_index(int i) { return i == 0 ? 0x045e : 0; }
static uint16_t product_index(int i) { return i == 0 ? 0x028e : 0; }
static uint16_t version_index(int i) { return i == 0 ? 0x0114 : 0; }
static uint16_t vendor(void *p) { return p == &handle ? 0x045e : 0; }
static uint16_t product(void *p) { return p == &handle ? 0x028e : 0; }
static uint16_t version(void *p) { return p == &handle ? 0x0114 : 0; }
static int type_index(int i) { return i == 0 ? 1 : 0; }
static int type_handle(void *p) { return p == &handle ? 1 : 0; }

int32_t SDL_DYNAPI_entry(uint32_t version_number, void *table, uint32_t size) {
    /* dlopen(NULL) finds the original entry exported by Unity, not this
       RTLD_LOCAL plugin. Calling it initializes the original SDL jump table. */
    void *main = dlopen(NULL, RTLD_NOW);
    int32_t (*original)(uint32_t, void *, uint32_t) = dlsym(main, "SDL_DYNAPI_entry");
    if (!original || (void *)original == (void *)&SDL_DYNAPI_entry) {
        fprintf(stderr, "lab-gamepad: cannot find player's SDL entry\n"); return -1;
    }
    int result = original(version_number, table, size);
    if (result || version_number != 1 || size < (SLOT_SDL_JoystickClose + 1) * sizeof(void *)) return -1;
    void **slots = table;
    real_pump = slots[SLOT_SDL_PumpEvents];
    real_update = slots[SLOT_SDL_JoystickUpdate];
    real_poll = slots[SLOT_SDL_PollEvent];
    real_push = slots[SLOT_SDL_PushEvent];
    real_was_init = slots[SLOT_SDL_WasInit];
#define HOOK(fn, replacement) do { if ((SLOT_##fn + 1) * sizeof(void *) <= size) slots[SLOT_##fn] = (void *)(replacement); } while (0)
    HOOK(SDL_PumpEvents, pump);
    HOOK(SDL_PollEvent, poll_event);
    HOOK(SDL_NumJoysticks, num);
    HOOK(SDL_IsGameController, not_controller);
    HOOK(SDL_JoystickNameForIndex, name_index);
    HOOK(SDL_JoystickOpen, open_index);
    HOOK(SDL_JoystickName, name);
    HOOK(SDL_JoystickGetDeviceGUID, device_guid);
    HOOK(SDL_JoystickGetGUID, get_guid);
    HOOK(SDL_JoystickGetAttached, attached);
    HOOK(SDL_JoystickInstanceID, instance);
    HOOK(SDL_JoystickGetDeviceInstanceID, device_instance);
    HOOK(SDL_JoystickNumAxes, axes);
    HOOK(SDL_JoystickNumBalls, zero_handle);
    HOOK(SDL_JoystickNumHats, hats);
    HOOK(SDL_JoystickNumButtons, buttons);
    HOOK(SDL_JoystickUpdate, update);
    HOOK(SDL_JoystickGetAxis, axis);
    HOOK(SDL_JoystickGetHat, hat);
    HOOK(SDL_JoystickGetBall, ball);
    HOOK(SDL_JoystickGetButton, button);
    HOOK(SDL_JoystickClose, close_handle);
    HOOK(SDL_JoystickIsHaptic, zero_handle);
    HOOK(SDL_JoystickCurrentPowerLevel, zero_handle);
    HOOK(SDL_JoystickFromInstanceID, from_instance);
    HOOK(SDL_JoystickGetDeviceVendor, vendor_index);
    HOOK(SDL_JoystickGetDeviceProduct, product_index);
    HOOK(SDL_JoystickGetDeviceProductVersion, version_index);
    HOOK(SDL_JoystickGetVendor, vendor);
    HOOK(SDL_JoystickGetProduct, product);
    HOOK(SDL_JoystickGetProductVersion, version);
    HOOK(SDL_JoystickGetAxisInitialState, initial);
    HOOK(SDL_JoystickGetDeviceType, type_index);
    HOOK(SDL_JoystickGetType, type_handle);
#undef HOOK
    fprintf(stderr, "lab-gamepad: SDL ABI %u, %u slots, virtual joystick enabled\n", version_number, (unsigned)(size / sizeof(void *)));
    return 0;
}
