#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static void caught(int signal) { (void)signal; }

static int check(const char *path, int flags, int restart, int through_proc)
{
    if (mkfifo(path, 0600)) { perror("mkfifo"); return 2; }
    struct sigaction action = { .sa_handler = caught, .sa_flags = restart ? SA_RESTART : 0 };
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGUSR1, &action, NULL)) return 2;
    pid_t child = fork();
    if (child < 0) return 2;
    if (child == 0) {
        alarm(5);
        char alias[80];
        if (through_proc) {
            int original = open(path, O_PATH);
            if (original < 0) _exit(2);
            snprintf(alias, sizeof(alias), "/proc/thread-self/fd/%d", original);
            path = alias;
        }
        int fd = open(path, flags | O_CLOEXEC);
        if ((restart && fd < 0) || (!restart && (fd >= 0 || errno != EINTR))) {
            fprintf(stderr, "FIFO open flags=%d SA_RESTART=%d proc=%d: fd=%d errno=%s\n",
                    flags, restart, through_proc, fd, strerror(errno));
            _exit(1);
        }
        if (fd >= 0) close(fd);
        _exit(0);
    }
    usleep(200000);
    kill(child, SIGUSR1);
    usleep(200000);
    int peer = open(path, (flags == O_WRONLY ? O_RDONLY : O_WRONLY) | O_NONBLOCK);
    int status;
    waitpid(child, &status, 0);
    if (peer >= 0) close(peer);
    unlink(path);
    if (!WIFEXITED(status) || WEXITSTATUS(status)) return 1;
    return 0;
}

int main(int argc, char **argv)
{
    if (argc != 2) return 2;
    for (int restart = 0; restart <= 1; restart++)
        for (int proc = 0; proc <= 1; proc++)
            for (int writer = 0; writer <= 1; writer++)
                if (check(argv[1], writer ? O_WRONLY : O_RDONLY, restart, proc)) return 1;
    puts("FIFO read/write opens obey SA_RESTART, directly and through proc");
    return 0;
}
