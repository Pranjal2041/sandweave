/* Probe for sdl-gamepad-proxy-check.py: a real SDL2 program, linked to the
 * system libSDL2 and given the proxy through SDL_DYNAMIC_API. It requests
 * rumble once, then samples every joystick every 20 ms as JSON lines on stdout.
 * SDL2 declarations are written out so only the runtime library is needed.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

typedef struct SDL_Joystick SDL_Joystick;
extern int SDL_Init(uint32_t flags);
extern const char *SDL_GetError(void);
extern int SDL_NumJoysticks(void);
extern SDL_Joystick *SDL_JoystickOpen(int index);
extern const char *SDL_JoystickName(SDL_Joystick *joystick);
extern void SDL_JoystickUpdate(void);
extern uint8_t SDL_JoystickGetButton(SDL_Joystick *joystick, int button);
extern int16_t SDL_JoystickGetAxis(SDL_Joystick *joystick, int axis);
extern int SDL_JoystickHasRumble(SDL_Joystick *joystick);
extern int SDL_JoystickHasRumbleTriggers(SDL_Joystick *joystick);
extern int SDL_JoystickRumble(SDL_Joystick *joystick, uint16_t low, uint16_t high, uint32_t ms);
extern int SDL_JoystickRumbleTriggers(SDL_Joystick *joystick, uint16_t left, uint16_t right, uint32_t ms);
extern void SDL_Quit(void);

static uint64_t now_ns(void) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * 1000000000 + now.tv_nsec;
}

int main(int argc, char **argv) {
    double seconds = argc > 1 ? atof(argv[1]) : 6;
    if (SDL_Init(0x200) != 0) { /* SDL_INIT_JOYSTICK */
        fprintf(stderr, "SDL_Init: %s\n", SDL_GetError());
        return 1;
    }
    int count = SDL_NumJoysticks();
    SDL_Joystick *pads[4] = {0};
    if (count < 1 || count > 4) {
        fprintf(stderr, "unexpected joystick count %d: %s\n", count, SDL_GetError());
        return 1;
    }
    for (int p = 0; p < count; ++p)
        if (!(pads[p] = SDL_JoystickOpen(p))) { fprintf(stderr, "open %d: %s\n", p, SDL_GetError()); return 1; }
    uint64_t requested = now_ns();
    /* Pad 0 requests motors, triggers and a stop; the last pad requests motors. */
    int rumble = SDL_JoystickRumble(pads[0], 0xFFFF, 0x8000, 250);
    int triggers = SDL_JoystickRumbleTriggers(pads[0], 0x4000, 0, 100);
    int stop = SDL_JoystickRumble(pads[0], 0, 0, 0);
    int last = SDL_JoystickRumble(pads[count - 1], 0x1000, 0x2000, 50);
    printf("{\"joysticks\":%d,\"name\":\"%s\",\"has_rumble\":%d,\"has_rumble_triggers\":%d,"
           "\"rumble\":%d,\"rumble_triggers\":%d,\"rumble_stop\":%d,\"rumble_last_pad\":%d,\"requested_ns\":%llu}\n",
           count, SDL_JoystickName(pads[0]), SDL_JoystickHasRumble(pads[0]), SDL_JoystickHasRumbleTriggers(pads[0]),
           rumble, triggers, stop, last, (unsigned long long)requested);
    fflush(stdout);
    uint64_t end = now_ns() + (uint64_t)(seconds * 1e9);
    while (now_ns() < end) {
        SDL_JoystickUpdate();
        printf("{\"t_ns\":%llu,\"pads\":[", (unsigned long long)now_ns());
        for (int p = 0; p < count; ++p) {
            uint32_t buttons = 0;
            for (int i = 0; i < 15; ++i)
                buttons |= (uint32_t)(SDL_JoystickGetButton(pads[p], i) & 1) << i;
            printf("%s{\"buttons\":%u,\"axes\":[", p ? "," : "", buttons);
            for (int i = 0; i < 8; ++i)
                printf(i ? ",%d" : "%d", SDL_JoystickGetAxis(pads[p], i));
            printf("]}");
        }
        printf("]}\n");
        fflush(stdout);
        struct timespec pause = {0, 20000000};
        nanosleep(&pause, NULL);
    }
    SDL_Quit();
    return 0;
}
