/* Userspace SDL2 joystick for disposable Unity/Linux lab processes.
 * Build with build-sdl-gamepad-proxy.py; opt in with SDL_DYNAMIC_API.
 * The player's own SDL implements every function except this joystick API.
 * No evdev/uinput device, game assembly patch, or host privilege is required.
 * Force feedback the game requests is appended as fixed records to
 * LAB_GAMEPAD_HAPTICS for the gamepad writer to report: SDL_JoystickRumble and
 * RumbleTriggers, and on each pad's SDL_Haptic device (SDL_HapticOpenFromJoystick)
 * SDL_HapticRumblePlay/Stop and constant-force effects (New/Update/StopEffect).
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

/* Slot numbers must match a Linux SDL build, which includes its __LINUX__-only procedures. */
#if defined(__linux__) && !defined(__LINUX__)
#define __LINUX__ 1
#endif
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
/* Up to four Xbox 360 pads (LAB_GAMEPAD_COUNT). As on Linux SDL with xpad, raw
   axes 2 and 5 are the left and right triggers, -32768 when released; SDL's own
   controller database maps this GUID's a2/a5 to lefttrigger/righttrigger. Pad 0
   reads LAB_GAMEPAD_STATE (default /tmp/lab-gamepad.bin), pad N "<that>-N". */
#define MAX_PADS 4
static const State neutral = {.axes = {0, 0, -32768, 0, 0, -32768, 0, 0}};
static State state[MAX_PADS];
static State emitted[MAX_PADS];
static uint64_t seen_sequence[MAX_PADS];
static int handles[MAX_PADS];
static int pad_count = 1;
static void (*real_pump)(void);
static void (*real_update)(void);
static int (*real_poll)(void *);
static int (*real_push)(void *);
static uint32_t (*real_was_init)(uint32_t);
static int announced;
static const Guid guid = {{3,0,0,0,0x5e,4,0,0,0x8e,2,0,0,0x14,1,0,0}};

static int pad(void *p) {
    for (int i = 0; i < pad_count; ++i) if (p == &handles[i]) return i;
    return -1;
}
static void refresh(void) {
    const char *base = getenv("LAB_GAMEPAD_STATE");
    if (!base) base = "/tmp/lab-gamepad.bin";
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    uint64_t ns = (uint64_t)now.tv_sec * 1000000000 + now.tv_nsec;
    for (int i = 0; i < pad_count; ++i) {
        char path[4096];
        snprintf(path, sizeof(path), i ? "%s-%d" : "%s", base, i);
        State next;
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        ssize_t size = fd < 0 ? -1 : read(fd, &next, sizeof(next));
        if (fd >= 0) close(fd);
        if (size == sizeof(next) && !memcmp(next.magic, "LABPAD1\0", 8) && next.expires_ns > ns) {
            state[i] = next;
            if (next.sequence != seen_sequence[i]) {
                seen_sequence[i] = next.sequence;
                fprintf(stderr, "lab-gamepad: pad=%d sequence=%lu buttons=0x%x axes=%d,%d,%d,%d,%d,%d,%d,%d\n", i,
                        next.sequence, next.buttons, next.axes[0], next.axes[1], next.axes[2], next.axes[3],
                        next.axes[4], next.axes[5], next.axes[6], next.axes[7]);
            }
        } else state[i] = neutral;
    }
}
static int num(void) { return pad_count; }
static const char *name_index(int i) { return i >= 0 && i < pad_count ? "Lab Virtual Gamepad" : NULL; }
static void *open_index(int i) {
    if (i < 0 || i >= pad_count) return NULL;
    fprintf(stderr, "lab-gamepad: joystick %d opened\n", i);
    return &handles[i];
}
static const char *name(void *p) { return name_index(pad(p)); }
static Guid device_guid(int i) { (void)i; return guid; }
static Guid get_guid(void *p) { (void)p; return guid; }
static int attached(void *p) { return pad(p) >= 0; }
static int instance(void *p) { return pad(p); }
static int device_instance(int i) { return i >= 0 && i < pad_count ? i : -1; }
static int axes(void *p) { return pad(p) >= 0 ? 8 : 0; }
static int zero_handle(void *p) { (void)p; return 0; }
static int hats(void *p) { return pad(p) >= 0 ? 1 : 0; }
static int buttons(void *p) { return pad(p) >= 0 ? 15 : 0; }
static void emit(uint32_t type, int which, int index, int value) {
    /* SDL2 SDL_Event is 56 bytes on this x86_64 ABI. Joystick events share
       type, timestamp and instance id (which), then an axis/hat/button byte. */
    union { uint64_t align; uint8_t bytes[56]; } event = {0};
    int32_t instance_id = which;
    memcpy(event.bytes, &type, 4);
    memcpy(event.bytes + 8, &instance_id, 4);
    event.bytes[12] = index;
    if (type == 0x600) {
        int16_t axis_value = value;
        memcpy(event.bytes + 16, &axis_value, 2);
    } else event.bytes[13] = value;
    real_push(&event);
}
static int poll_event(void *event) {
    State before[MAX_PADS];
    memcpy(before, emitted, sizeof(before));
    refresh();
    if (!announced && (real_was_init(0x200) & 0x200)) {
        announced = 1;
        for (int p = 0; p < pad_count; ++p) {
            emit(0x605, p, 0, 0);  /* SDL_JOYDEVICEADDED: which is the device index */
            emitted[p] = before[p] = neutral;
        }
        fprintf(stderr, "lab-gamepad: device-added events for %d pads\n", pad_count);
    }
    if (announced) {
        for (int p = 0; p < pad_count; ++p) {
            for (int i = 0; i < 8; ++i)
                if (before[p].axes[i] != state[p].axes[i]) emit(0x600, p, i, state[p].axes[i]);
            for (int i = 0; i < 15; ++i)
                if (((before[p].buttons ^ state[p].buttons) >> i) & 1)
                    emit((state[p].buttons >> i) & 1 ? 0x603 : 0x604, p, i, (state[p].buttons >> i) & 1);
            if (before[p].hat != state[p].hat) emit(0x602, p, 0, state[p].hat);
            emitted[p] = state[p];
        }
    }
    return real_poll(event);
}
static void update(void) { refresh(); if (real_update) real_update(); }
static void pump(void) { refresh(); if (real_pump) real_pump(); }
static int16_t axis(void *p, int i) { int n = pad(p); return n >= 0 && i >= 0 && i < 8 ? state[n].axes[i] : 0; }
static uint8_t hat(void *p, int i) { int n = pad(p); return n >= 0 && i == 0 ? state[n].hat : 0; }
static uint8_t button(void *p, int i) { int n = pad(p); return n >= 0 && i >= 0 && i < 15 ? (state[n].buttons >> i) & 1 : 0; }
static int ball(void *p, int i, int *x, int *y) { (void)p; (void)i; if(x)*x=0; if(y)*y=0; return -1; }
static void close_handle(void *p) { (void)p; }
static int initial(void *p, int i, int16_t *v) { if (v) *v=(i >= 0 && i < 8) ? neutral.axes[i] : 0; return pad(p) >= 0 && i >= 0 && i < 8; }
static int not_controller(int i) { (void)i; return 0; }
static void *from_instance(int i) { return i >= 0 && i < pad_count ? &handles[i] : NULL; }
static uint16_t vendor_index(int i) { return i >= 0 && i < pad_count ? 0x045e : 0; }
static uint16_t product_index(int i) { return i >= 0 && i < pad_count ? 0x028e : 0; }
static uint16_t version_index(int i) { return i >= 0 && i < pad_count ? 0x0114 : 0; }
static uint16_t vendor(void *p) { return pad(p) >= 0 ? 0x045e : 0; }
static uint16_t product(void *p) { return pad(p) >= 0 ? 0x028e : 0; }
static uint16_t version(void *p) { return pad(p) >= 0 ? 0x0114 : 0; }
static int type_index(int i) { return i >= 0 && i < pad_count ? 1 : 0; }
static int type_handle(void *p) { return pad(p) >= 0 ? 1 : 0; }

typedef struct __attribute__((packed)) {
    char magic[4];          /* "RUM1" */
    uint16_t output;        /* 0 motors (low, high frequency), 1 triggers (left, right),
                               2 haptic rumble (strength), 3 haptic constant force (level as int16) */
    uint16_t pad;           /* 0..3 */
    uint64_t monotonic_ns;  /* CLOCK_MONOTONIC when the game requested it */
    uint16_t first, second; /* strengths 0..0xFFFF; both zero stops */
    uint32_t duration_ms;
} Rumble;
_Static_assert(sizeof(Rumble) == 24, "gamepad haptics record ABI");
static int rumble_fd = -1;
static int haptic_handles[MAX_PADS];
static int record(uint16_t output, int n, uint16_t first, uint16_t second, uint32_t duration_ms) {
    if (n < 0 || rumble_fd < 0) return -1;
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    Rumble entry = {{'R', 'U', 'M', '1'}, output, (uint16_t)n, (uint64_t)now.tv_sec * 1000000000 + now.tv_nsec,
                    first, second, duration_ms};
    /* One write on an O_APPEND file: records from concurrent callers never interleave. */
    return write(rumble_fd, &entry, sizeof(entry)) == (ssize_t)sizeof(entry) ? 0 : -1;
}
static int rumble(void *p, uint16_t low, uint16_t high, uint32_t ms) { return record(0, pad(p), low, high, ms); }
static int rumble_triggers(void *p, uint16_t left, uint16_t right, uint32_t ms) { return record(1, pad(p), left, right, ms); }
static int has_rumble(void *p) { return pad(p) >= 0 && rumble_fd >= 0; }

/* SDL_Haptic on the virtual pads: one device per pad, rumble plus one constant-force effect (id 0). */
static int hpad(void *h) {
    for (int i = 0; i < pad_count; ++i) if (h == &haptic_handles[i]) return i;
    return -1;
}
static void *haptic_open(void *joystick) { int n = pad(joystick); return n >= 0 && rumble_fd >= 0 ? &haptic_handles[n] : NULL; }
static int joystick_is_haptic(void *p) { return pad(p) >= 0 && rumble_fd >= 0; }
static unsigned haptic_query(void *h) { return hpad(h) >= 0 ? 1u /* SDL_HAPTIC_CONSTANT */ : 0; }
static int haptic_num_axes(void *h) { return hpad(h) >= 0 ? 1 : -1; }
static int haptic_rumble_supported(void *h) { return hpad(h) >= 0; }
static int haptic_ok(void *h) { return hpad(h) >= 0 ? 0 : -1; }
static int haptic_rumble_play(void *h, float strength, uint32_t length) {
    float clamped = strength < 0 ? 0 : strength > 1 ? 1 : strength;
    return record(2, hpad(h), (uint16_t)(clamped * 0xFFFF + .5f), 0, length);
}
static int haptic_rumble_stop(void *h) { return record(2, hpad(h), 0, 0, 0); }
/* SDL_HapticConstant (x86_64): Uint16 type at 0, Uint32 length at 20, Sint16 level at 30. */
static int constant(const void *effect, int16_t *level, uint32_t *length) {
    uint16_t type;
    memcpy(&type, effect, 2);
    if (type != 1) return -1;  /* SDL_HAPTIC_CONSTANT */
    memcpy(length, (const char *)effect + 20, 4);
    memcpy(level, (const char *)effect + 30, 2);
    return 0;
}
static int haptic_effect_supported(void *h, void *effect) {
    int16_t level; uint32_t length;
    return hpad(h) >= 0 && effect && constant(effect, &level, &length) == 0;
}
static int haptic_set_effect(void *h, void *effect) {
    int16_t level; uint32_t length;
    if (hpad(h) < 0 || !effect || constant(effect, &level, &length)) return -1;
    return record(3, hpad(h), (uint16_t)level, 0, length) ? -1 : 0;  /* effect id 0 */
}
static int haptic_update_effect(void *h, int id, void *effect) { return id == 0 ? haptic_set_effect(h, effect) : -1; }
static int haptic_run_effect(void *h, int id, uint32_t iterations) { (void)iterations; return hpad(h) >= 0 && id == 0 ? 0 : -1; }
static int haptic_stop_effect(void *h, int id) { return id == 0 ? record(3, hpad(h), 0, 0, 0) : -1; }
static void haptic_destroy_effect(void *h, int id) { (void)h; (void)id; }
static void haptic_close(void *h) { (void)h; }

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
    const char *count = getenv("LAB_GAMEPAD_COUNT");
    pad_count = count ? atoi(count) : 1;
    if (pad_count < 1 || pad_count > MAX_PADS) {
        fprintf(stderr, "lab-gamepad: LAB_GAMEPAD_COUNT must be 1..%d\n", MAX_PADS); return -1;
    }
    for (int i = 0; i < pad_count; ++i) state[i] = emitted[i] = neutral;
    const char *haptics = getenv("LAB_GAMEPAD_HAPTICS");
    rumble_fd = open(haptics ? haptics : "/tmp/lab-gamepad-haptics.bin", O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0600);
    if (rumble_fd < 0) perror("lab-gamepad: haptics record unavailable");
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
    HOOK(SDL_JoystickIsHaptic, joystick_is_haptic);
    HOOK(SDL_HapticOpenFromJoystick, haptic_open);
    HOOK(SDL_HapticQuery, haptic_query);
    HOOK(SDL_HapticNumAxes, haptic_num_axes);
    HOOK(SDL_HapticEffectSupported, haptic_effect_supported);
    HOOK(SDL_HapticNewEffect, haptic_set_effect);
    HOOK(SDL_HapticUpdateEffect, haptic_update_effect);
    HOOK(SDL_HapticRunEffect, haptic_run_effect);
    HOOK(SDL_HapticStopEffect, haptic_stop_effect);
    HOOK(SDL_HapticDestroyEffect, haptic_destroy_effect);
    HOOK(SDL_HapticRumbleSupported, haptic_rumble_supported);
    HOOK(SDL_HapticRumbleInit, haptic_ok);
    HOOK(SDL_HapticRumblePlay, haptic_rumble_play);
    HOOK(SDL_HapticRumbleStop, haptic_rumble_stop);
    HOOK(SDL_HapticClose, haptic_close);
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
    HOOK(SDL_JoystickRumble, rumble);
    HOOK(SDL_JoystickRumbleTriggers, rumble_triggers);
    HOOK(SDL_JoystickHasRumble, has_rumble);
    HOOK(SDL_JoystickHasRumbleTriggers, has_rumble);
#undef HOOK
    fprintf(stderr, "lab-gamepad: SDL ABI %u, %u slots, virtual joystick enabled\n", version_number, (unsigned)(size / sizeof(void *)));
    return 0;
}
