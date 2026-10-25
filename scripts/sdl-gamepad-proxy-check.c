/* Probe for sdl-gamepad-proxy-check.py: a real SDL2 program, linked to the
 * system libSDL2 and given the proxy through SDL_DYNAMIC_API. It requests
 * rumble once, then samples joystick 0 every 20 ms as JSON lines on stdout.
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
    SDL_Joystick *pad = SDL_NumJoysticks() > 0 ? SDL_JoystickOpen(0) : NULL;
    if (!pad) {
        fprintf(stderr, "no joystick: %s\n", SDL_GetError());
        return 1;
    }
    uint64_t requested = now_ns();
    int rumble = SDL_JoystickRumble(pad, 0xFFFF, 0x8000, 250);
    int triggers = SDL_JoystickRumbleTriggers(pad, 0x4000, 0, 100);
    int stop = SDL_JoystickRumble(pad, 0, 0, 0);
    printf("{\"joysticks\":%d,\"name\":\"%s\",\"has_rumble\":%d,\"has_rumble_triggers\":%d,"
           "\"rumble\":%d,\"rumble_triggers\":%d,\"rumble_stop\":%d,\"requested_ns\":%llu}\n",
           SDL_NumJoysticks(), SDL_JoystickName(pad), SDL_JoystickHasRumble(pad),
           SDL_JoystickHasRumbleTriggers(pad), rumble, triggers, stop, (unsigned long long)requested);
    fflush(stdout);
    uint64_t end = now_ns() + (uint64_t)(seconds * 1e9);
    while (now_ns() < end) {
        SDL_JoystickUpdate();
        uint32_t buttons = 0;
        for (int i = 0; i < 15; ++i)
            buttons |= (uint32_t)(SDL_JoystickGetButton(pad, i) & 1) << i;
        printf("{\"t_ns\":%llu,\"buttons\":%u,\"axes\":[", (unsigned long long)now_ns(), buttons);
        for (int i = 0; i < 8; ++i)
            printf(i ? ",%d" : "%d", SDL_JoystickGetAxis(pad, i));
        printf("]}\n");
        fflush(stdout);
        struct timespec pause = {0, 20000000};
        nanosleep(&pause, NULL);
    }
    SDL_Quit();
    return 0;
}
