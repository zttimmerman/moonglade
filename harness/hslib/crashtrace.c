/* crashtrace: a DYLD_INSERT_LIBRARIES shim that records the backtrace of THIS process's fatal SIGSEGV/SIGBUS.
 *
 * Used by core.login_logout_race_baseline (harness/hslib/crashtrace.py builds and reads it). macOS ReportCrash
 * cannot be trusted to write a new .ips for every crash: it coalesced repeat crashes of the same binary into an
 * older report, so the check could read stale frames. This shim writes the record itself, from inside the
 * crashing process, before the process dies:
 *
 *   $HS_CRASHTRACE_DIR/crash-<pid>.txt
 *     pid <pid>
 *     signo <n>, code <si_code>, addr <si_addr>, si_pid <sender>
 *     uuid <main executable LC_UUID>, slide <main executable ASLR slide>
 *     exc / esr / far: the thread's arm64 exception state (mcontext __es)
 *     pc, lr, then "frame <i> <addr>" lines: pc and the frame-pointer chain of the thread that got the signal
 *
 * Telling a fault from kill(): on this macOS, si_code and si_pid do NOT (measured 2026-10-03: `kill -SEGV`
 * arrives with si_code 2 = SEGV_ACCERR and si_pid 0, same as a real null dereference). The exception syndrome does:
 * ESR's exception class (bits 31:26) is 0x24/0x25 (data abort) for a real bad access, and was 0x15 (SVC, the thread
 * sat in a syscall) for a kill of an idle process. crashtrace.py reads it.
 *
 * After writing, the handler restores SIG_DFL and re-raises, so the process still dies of the original signal
 * (exit status -11 for SIGSEGV). Everything in the handler is async-signal-safe (open/write/close, raise, memory
 * reads). */
#include <fcntl.h>
#include <mach-o/dyld.h>
#include <mach-o/loader.h>
#include <signal.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ucontext.h>
#include <unistd.h>

static char g_path[1024];
static char g_uuid[40] = "unknown";
static uintptr_t g_slide;

static size_t put_str(char *b, size_t n, const char *s) { while (*s) b[n++] = *s++; return n; }

static size_t put_hex(char *b, size_t n, uint64_t v) {
    char t[17]; int i = 16; t[16] = 0;
    do { t[--i] = "0123456789abcdef"[v & 15]; v >>= 4; } while (v && i > 0);
    n = put_str(b, n, "0x");
    return put_str(b, n, t + i);
}

static size_t put_dec(char *b, size_t n, int64_t v) {
    char t[24]; int i = 23; t[23] = 0; int neg = v < 0; uint64_t u = neg ? (uint64_t)(-v) : (uint64_t)v;
    do { t[--i] = (char)('0' + u % 10); u /= 10; } while (u && i > 0);
    if (neg) t[--i] = '-';
    return put_str(b, n, t + i);
}

static void handler(int signo, siginfo_t *si, void *uctx) {
    static char buf[16384];
    size_t n = 0;
    n = put_str(buf, n, "pid "); n = put_dec(buf, n, getpid());
    n = put_str(buf, n, "\nsigno "); n = put_dec(buf, n, signo);
    n = put_str(buf, n, "\ncode "); n = put_dec(buf, n, si->si_code);
    n = put_str(buf, n, "\naddr "); n = put_hex(buf, n, (uint64_t)(uintptr_t)si->si_addr);
    n = put_str(buf, n, "\nuuid "); n = put_str(buf, n, g_uuid);
    n = put_str(buf, n, "\nslide "); n = put_hex(buf, n, g_slide);
    n = put_str(buf, n, "\n");
#if defined(__arm64__)
    ucontext_t *uc = (ucontext_t *)uctx;
    uint64_t pc = uc->uc_mcontext->__ss.__pc, lr = uc->uc_mcontext->__ss.__lr, fp = uc->uc_mcontext->__ss.__fp;
    n = put_str(buf, n, "si_pid "); n = put_dec(buf, n, si->si_pid);
    n = put_str(buf, n, "\nexc "); n = put_dec(buf, n, uc->uc_mcontext->__es.__exception);
    n = put_str(buf, n, "\nesr "); n = put_hex(buf, n, uc->uc_mcontext->__es.__esr);
    n = put_str(buf, n, "\nfar "); n = put_hex(buf, n, uc->uc_mcontext->__es.__far);
    n = put_str(buf, n, "\npc "); n = put_hex(buf, n, pc);
    n = put_str(buf, n, "\nlr "); n = put_hex(buf, n, lr);
    n = put_str(buf, n, "\nframe 0 "); n = put_hex(buf, n, pc); n = put_str(buf, n, "\n");
    /* Frame-pointer chain: [fp] = caller's fp, [fp+8] = return address. Stop on a null, misaligned or
     * non-increasing fp (stacks grow down, so callers' frames sit at higher addresses). */
    for (int i = 1; i < 64 && fp && !(fp & 7); i++) {
        uint64_t *f = (uint64_t *)(uintptr_t)fp;
        uint64_t next = f[0], ret = f[1];
        if (!ret) break;
        n = put_str(buf, n, "frame "); n = put_dec(buf, n, i); n = put_str(buf, n, " ");
        n = put_hex(buf, n, ret & 0x0000ffffffffffffULL); n = put_str(buf, n, "\n");
        if (next <= fp) break;
        fp = next;
    }
#endif
    n = put_str(buf, n, "end\n");
    if (g_path[0]) {
        int fd = open(g_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd >= 0) { (void)write(fd, buf, n); close(fd); }
    }
    signal(signo, SIG_DFL);
    raise(signo);    /* blocked while the handler runs: delivered on return, and the default action kills us */
}

__attribute__((constructor)) static void crashtrace_init(void) {
    const char *dir = getenv("HS_CRASHTRACE_DIR");
    if (!dir || strlen(dir) > 900) return;
    size_t n = put_str(g_path, 0, dir);
    n = put_str(g_path, n, "/crash-"); n = put_dec(g_path, n, getpid()); n = put_str(g_path, n, ".txt");
    g_path[n] = 0;
    /* The main executable (the MH_EXECUTE image; with DYLD_INSERT_LIBRARIES it is not always index 0): its slide
     * and LC_UUID, read now so the handler only does memory reads. */
    const struct mach_header_64 *mh = NULL;
    for (uint32_t i = 0; i < _dyld_image_count(); i++) {
        const struct mach_header_64 *h = (const struct mach_header_64 *)_dyld_get_image_header(i);
        if (h && h->magic == MH_MAGIC_64 && h->filetype == MH_EXECUTE) {
            mh = h;
            g_slide = (uintptr_t)_dyld_get_image_vmaddr_slide(i);
            break;
        }
    }
    if (mh) {
        const uint8_t *p = (const uint8_t *)(mh + 1);
        for (uint32_t i = 0; i < mh->ncmds; i++) {
            const struct load_command *lc = (const struct load_command *)p;
            if (lc->cmd == LC_UUID) {
                const uint8_t *u = ((const struct uuid_command *)lc)->uuid;
                size_t k = 0;
                for (int j = 0; j < 16; j++) {
                    if (j == 4 || j == 6 || j == 8 || j == 10) g_uuid[k++] = '-';
                    g_uuid[k++] = "0123456789abcdef"[u[j] >> 4];
                    g_uuid[k++] = "0123456789abcdef"[u[j] & 15];
                }
                g_uuid[k] = 0;
                break;
            }
            p += lc->cmdsize;
        }
    }
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = handler;
    sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
    sigemptyset(&sa.sa_mask);
    /* An alternate stack, so a fault from stack exhaustion can still be recorded. */
    static char altstack[65536];
    stack_t ss = {.ss_sp = altstack, .ss_size = sizeof altstack, .ss_flags = 0};
    sigaltstack(&ss, NULL);
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
}
