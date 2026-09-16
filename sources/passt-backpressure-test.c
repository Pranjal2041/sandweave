/* Exercise passt's real TCP buffering with a short output flush. */
#include <assert.h>
#include <stdio.h>
#include <sys/socket.h>
#include "tcp_buf.c"

static unsigned flushes, reads;
static uint32_t expected_skip;
size_t __wrap_tap_send_frames(const struct ctx *c, const struct iovec *iov,
                             size_t nbufs, size_t nframes)
{ (void)c; (void)iov; (void)nbufs; (void)nframes; flushes++; return 0; }
int __wrap_tcp_set_peek_offset(const struct tcp_tap_conn *conn, int offset)
{ (void)conn; (void)offset; return 0; }
void __wrap_tcp_rst_do(const struct ctx *c, struct tcp_tap_conn *conn)
{ (void)c; (void)conn; assert(!"unexpected TCP reset"); }
void __wrap_conn_flag_do(const struct ctx *c, struct tcp_tap_conn *conn, unsigned long flag)
{ (void)c; (void)conn; (void)flag; }
ssize_t __wrap_recvmsg(int fd, struct msghdr *msg, int flags)
{
    (void)fd; (void)flags;
    reads++;
    if (msg->msg_iov[0].iov_len != expected_skip) {
        fprintf(stderr,"stale TCP payload offset: got %zu, expected %u\n",msg->msg_iov[0].iov_len,expected_skip);
        exit(1);
    }
    errno=EAGAIN; return -1;
}
int main(void)
{
    struct ctx c = { .mode = MODE_PASST };
    struct tcp_tap_conn conn = { .seq_to_tap = 10000, .seq_ack_from_tap = 0,
                                .wnd_from_tap = 65535, .ws_from_tap = 0 };
    MSS_SET((&conn),1460);
    peek_offset_cap=false;
    tcp_sock_iov_init(&c);
    tcp_payload_used=TCP_FRAMES_MEM-1;
    for (unsigned i=0;i<tcp_payload_used;i++) {
        tcp_frame_conns[i]=&conn;
        tcp_payload[i].th.seq=htonl(3000+i);
    }
    expected_skip=3000;
    tcp_buf_data_from_sock(&c,&conn);
    assert(flushes==1 && reads==1);
    puts("partial flush recalculates the TCP payload offset");
    return 0;
}
