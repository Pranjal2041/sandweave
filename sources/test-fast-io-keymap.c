/* Exercise the allocator itself without an X server. */
#define main bridge_main
#include "xvnc-fast-io.c"
#undef main
#include <assert.h>

int main(void) {
    first_key = 8; last_key = 11; columns = 2;
    uint32_t symbols[] = {'a', 'A', 0x1004e00, 0, 0x1004e01, 0, 0x1004e02, 0};
    assigned[9] = symbols[2]; assigned[10] = symbols[4]; assigned[11] = symbols[6];
    last_used[9] = 1; last_used[10] = 2; last_used[11] = 3;
    uint8_t pressed[32] = {0}, codes[4]; uint32_t planned[256];
    pressed[10 / 8] |= 1u << (10 % 8);
    struct event events[] = {{4, 0x1004e03, 0, 0}, {5, 0x1004e03, 0, 0},
                             {4, 0x1004e00, 0, 0}, {5, 0x1004e00, 0, 0}};
    /* Key 9 is needed later; key 10 is held. Only key 11 may be recycled. */
    assert(!plan_batch(events, 4, 100, 100, symbols, pressed, codes, planned));
    assert(codes[0] == 11 && codes[1] == 11 && codes[2] == 9 && codes[3] == 9);
    assert(planned[11] == 0x1004e03 && assigned[11] == 0x1004e02);
    /* A batch that cannot fit is rejected; the committed cache stays intact. */
    struct event oversized[] = {{4, 0x1004e04, 0, 0}, {4, 0x1004e05, 0, 0},
                                {4, 0x1004e06, 0, 0}, {4, 0x1004e07, 0, 0}};
    assert(plan_batch(oversized, 4, 100, 100, symbols, pressed, codes, planned));
    assert(assigned[9] == 0x1004e00 && assigned[10] == 0x1004e01 && assigned[11] == 0x1004e02);
    puts("keymap reservation, held-key protection and atomic planning passed");
    return 0;
}
