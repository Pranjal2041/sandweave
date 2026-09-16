/* Exercise passt's real receive path after a queued acknowledgement was lost. */
#include <assert.h>
#include <stdio.h>
#include "tcp.c"

static unsigned forced_acks;
int __wrap_tcp_buf_send_flag(const struct ctx *c, struct tcp_tap_conn *conn,
                             int flags)
{
    (void)c; (void)conn;
    if (flags & ACK)
        forced_acks++;
    return 0;
}

static void retransmit(uint32_t seq, size_t length)
{
    struct ctx c = { .mode = MODE_PASST };
    struct tcp_tap_conn conn = { .events = ESTABLISHED, .wnd_from_tap = 65535,
                                .seq_from_tap = seq + length,
                                .seq_ack_to_tap = seq + length };
    struct { struct tcphdr header; char body[113]; } packet = { 0 };
    PACKET_POOL_DECL(single, 1) pool = {
        .buf = (char *)&packet, .buf_size = sizeof(packet), .size = 1, .count = 1,
        .pkt = {{ .iov_base = &packet, .iov_len = sizeof(packet.header) + length }}
    };
    packet.header.doff = sizeof(packet.header) / 4;
    packet.header.seq = htonl(seq);
    forced_acks = 0;
    assert(tcp_data_from_tap(&c, &conn, (struct pool *)&pool, 0) == 1);
    assert(conn.seq_from_tap == (uint32_t)(seq + length));
    if (forced_acks != (length ? 1U : 0U)) {
        fprintf(stderr, "retransmitted payload must receive an ACK (length=%zu, ACKs=%u)\n",
                length, forced_acks);
        exit(1);
    }
}

int main(void)
{
    retransmit(2725883852U, 113);
    retransmit(UINT32_MAX - 20, 113);
    retransmit(1000, 0); /* Do not create ACK loops for empty segments. */
    puts("retransmitted payload is acknowledged even after a queued ACK was lost");
    return 0;
}
