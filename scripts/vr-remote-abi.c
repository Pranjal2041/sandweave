/* Compare this upstream C layout with vr-remote-input.py on the test sandbox.
 * Include paths: monado-source/src/xrt/{drivers,include,auxiliary} and
 * monado-build/src/xrt/include. No Monado libraries are linked. */
#include "remote/r_interface.h"
#include <stddef.h>
#include <stdio.h>

int main(void)
{
    printf("{\"packet\":%zu,\"head\":%zu,\"controller\":%zu,"
           "\"left_offset\":%zu,\"right_offset\":%zu,\"trigger_offset\":%zu}\n",
           sizeof(struct r_remote_data), sizeof(struct r_head_data),
           sizeof(struct r_remote_controller_data), offsetof(struct r_remote_data, left),
           offsetof(struct r_remote_data, right), offsetof(struct r_remote_controller_data, trigger_click));
    return 0;
}
