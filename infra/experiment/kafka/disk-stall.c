#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

/* Block data-file writes/flushes, not the process, sockets or KRaft metadata.
 * Use raw syscalls in the gate to avoid recursion through interposed functions.
 * Expiry is enforced inside every waiting thread, even if the runner disappears. */
static double expiry(void) {
    char buf[64] = {0};
    int f = syscall(SYS_openat, AT_FDCWD, "/faults/kafka1-until", O_RDONLY, 0);
    if (f < 0) return 0;
    long n = syscall(SYS_read, f, buf, sizeof(buf)-1);
    syscall(SYS_close, f);
    return n > 0 ? strtod(buf, NULL) : 0;
}
static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}
static void event(const char *state, const char *op, int fd) {
    char buf[200];
    int n = snprintf(buf, sizeof(buf), "%.6f %s %s %d\n", now(), state, op, fd);
    int f = syscall(SYS_openat, AT_FDCWD, "/faults/disk-events.log", O_WRONLY|O_CREAT|O_APPEND, 0666);
    if (f >= 0) { syscall(SYS_write, f, buf, n); syscall(SYS_close, f); }
}
static void gate(int fd, const char *op) {
    int saved = errno;
    if (expiry() <= now()) { errno = saved; return; }
    char link[64], path[4096];
    snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
    ssize_t n = readlink(link, path, sizeof(path)-1);
    if (n <= 0) { errno = saved; return; }
    path[n] = 0;
    const char *prefix = "/var/lib/kafka/data/";
    if (strncmp(path, prefix, strlen(prefix)) != 0) { errno = saved; return; }
    event("blocked", op, fd);
    while (expiry() > now()) {
        struct timespec pause = {.tv_sec=0, .tv_nsec=20000000};
        nanosleep(&pause, NULL);
    }
    event("released", op, fd);
    errno = saved;
}
#define WRAP_WRITE(name, offset_type) \
ssize_t name(int fd, const void *buf, size_t count, offset_type offset) { \
    gate(fd, #name); \
    ssize_t (*real)(int,const void*,size_t,offset_type) = dlsym(RTLD_NEXT, #name); \
    return real(fd,buf,count,offset); \
}
ssize_t write(int fd, const void *buf, size_t count) {
    gate(fd,"write");
    ssize_t (*real)(int,const void*,size_t) = dlsym(RTLD_NEXT,"write");
    return real(fd,buf,count);
}
WRAP_WRITE(pwrite, off_t)
#undef pwrite64
WRAP_WRITE(pwrite64, off_t)
ssize_t writev(int fd, const struct iovec *iov, int count) {
    gate(fd,"writev");
    ssize_t (*real)(int,const struct iovec*,int) = dlsym(RTLD_NEXT,"writev");
    return real(fd,iov,count);
}
int fsync(int fd) {
    gate(fd,"fsync");
    int (*real)(int) = dlsym(RTLD_NEXT,"fsync");
    return real(fd);
}
int fdatasync(int fd) {
    gate(fd,"fdatasync");
    int (*real)(int) = dlsym(RTLD_NEXT,"fdatasync");
    return real(fd);
}
