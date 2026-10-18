/*
 * agyhook — in-process interception of agy's outbound HTTPS plaintext.
 *
 * Why this and not an IAT hook: agy.exe imports only KERNEL32/ADVAPI32/bcrypt/
 * dbghelp. Go issues syscalls directly and its TLS is pure Go (no BoringSSL
 * symbols in the binary), so there is no C-level network function to trampoline.
 * The only place the plaintext exists is inside the Go runtime.
 *
 * Target: crypto/tls.(*Conn).Write  (RVA 0x3fd340)
 *   func (c *Conn) Write(b []byte) (n int, err error)
 * At the entry breakpoint nothing has executed yet, so the Go register ABI
 * still holds the arguments. We do not assume which register is which: we scan
 * the GPRs for a plausible (ptr,len) slice whose bytes look like our request.
 *
 * INT3 (1 byte) rather than a JMP trampoline: every Go function opens with a
 * stack-growth check (CMPQ SP,16(R14); JBE morestack). Overwriting it with a
 * 5-byte JMP corrupts that path; a breakpoint leaves the code intact.
 */

#include <windows.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>

#define TARGET_RVA      0x3fe340u   /* crypto/tls.(*Conn).Write */
#define MAX_CAPTURE     (1u << 20)  /* 1 MiB per write */

static BYTE  *g_target      = NULL;   /* absolute address of the patched byte */
static BYTE   g_original    = 0;      /* byte we replaced with INT3 */
static HANDLE g_log         = INVALID_HANDLE_VALUE;
static volatile LONG g_hits = 0;
static volatile LONG g_rearm = 0;
static int g_orig_valid = 0;

/* ---------------------------------------------------------------- helpers */

static void log_line(const char *fmt, ...) {
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    int n = wvsprintfA(buf, fmt, ap);
    va_end(ap);
    if (n > 0) WriteFile(g_log, buf, (DWORD)n, NULL, NULL);
}

static int readable(const void *p, size_t n) {
    MEMORY_BASIC_INFORMATION mbi;
    if (!p || !VirtualQuery(p, &mbi, sizeof(mbi))) return 0;
    if (mbi.State != MEM_COMMIT) return 0;
    if (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD)) return 0;
    if (mbi.Protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
                       PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE |
                       PAGE_EXECUTE_WRITECOPY)) {
        size_t avail = (BYTE *)mbi.BaseAddress + mbi.RegionSize - (BYTE *)p;
        return avail >= n;
    }
    return 0;
}

/* A TLS write carries the serialized HTTP request: request line, headers, then
 * body. Recognize ours without assuming anything else about the connection. */
static int looks_like_request(const BYTE *p, DWORD len) {
    if (len < 16 || !readable(p, 16)) return 0;
    if (memcmp(p, "POST ", 5) && memcmp(p, "GET ", 4)) return 0;
    /* Only the Cloud Code calls interest us. */
    for (DWORD i = 0; i + 20 < len && i < 4096; i++) {
        if (!memcmp(p + i, "/v1internal:", 12)) return 1;
    }
    return 0;
}

static void install_breakpoint(void) {
    DWORD old;
    VirtualProtect(g_target, 1, PAGE_EXECUTE_READWRITE, &old);
    /* Capture the real byte exactly once. Re-reading it on every re-arm is a
     * race: if the re-arm lands while the byte is still 0xCC we would record
     * 0xCC as "original" and could never restore the function again. */
    if (!g_orig_valid) { g_original = *g_target; g_orig_valid = 1; }
    *g_target = 0xCC;                     /* INT3 */
    VirtualProtect(g_target, 1, old, &old);
    FlushInstructionCache(GetCurrentProcess(), g_target, 1);
}

static void remove_breakpoint(void) {
    DWORD old;
    VirtualProtect(g_target, 1, PAGE_EXECUTE_READWRITE, &old);
    *g_target = g_original;
    VirtualProtect(g_target, 1, old, &old);
    FlushInstructionCache(GetCurrentProcess(), g_target, 1);
}

/* ------------------------------------------------------------- forwarding */

/* A vectored exception handler must not block -- no sockets, no waiting. It
 * copies the captured bytes into a fixed ring and returns; a background thread
 * drains it and POSTs each request to AGYHOOK_FORWARD (host:port). */

#define QSLOTS 64

typedef struct { DWORD len; BYTE *data; } QItem;

static QItem            g_queue[QSLOTS];
static volatile LONG    g_qhead = 0, g_qtail = 0;
static volatile LONG    g_dropped = 0;
static char             g_fwd_host[64];
static unsigned short   g_fwd_port = 0;
static volatile LONG    g_fwd_on = 0;

/* Minimal Winsock, resolved at runtime: agy itself imports no network DLL, and
 * we do not want to add a static dependency either. */
typedef UINT_PTR sock_t;
typedef struct { short family; unsigned short port; unsigned int addr; char pad[8]; } sockaddr_in_t;

static sock_t (WINAPI *p_socket)(int, int, int);
static int    (WINAPI *p_connect)(sock_t, const sockaddr_in_t *, int);
static int    (WINAPI *p_send)(sock_t, const char *, int, int);
static int    (WINAPI *p_recv)(sock_t, char *, int, int);
static int    (WINAPI *p_closesocket)(sock_t);
static int    (WINAPI *p_WSAStartup)(unsigned short, void *);
static unsigned long (WINAPI *p_inet_addr)(const char *);
static unsigned short (WINAPI *p_htons)(unsigned short);

static int load_winsock(void) {
    HMODULE ws = LoadLibraryA("ws2_32.dll");
    if (!ws) return 0;
    p_socket       = (void *)GetProcAddress(ws, "socket");
    p_connect      = (void *)GetProcAddress(ws, "connect");
    p_send         = (void *)GetProcAddress(ws, "send");
    p_recv         = (void *)GetProcAddress(ws, "recv");
    p_closesocket  = (void *)GetProcAddress(ws, "closesocket");
    p_WSAStartup   = (void *)GetProcAddress(ws, "WSAStartup");
    p_inet_addr    = (void *)GetProcAddress(ws, "inet_addr");
    p_htons        = (void *)GetProcAddress(ws, "htons");
    return p_socket && p_connect && p_send && p_closesocket && p_WSAStartup;
}

static int forward_one(const BYTE *data, DWORD len) {
    sock_t s = p_socket(2 /*AF_INET*/, 1 /*SOCK_STREAM*/, 0);
    if (s == (sock_t)-1) return 0;
    sockaddr_in_t sa = {0};
    sa.family = 2;
    sa.port   = p_htons(g_fwd_port);
    sa.addr   = p_inet_addr(g_fwd_host);
    if (p_connect(s, &sa, sizeof(sa)) != 0) { p_closesocket(s); return 0; }

    char head[256];
    int hn = wsprintfA(head,
                        "POST /agyhook HTTP/1.1\r\nHost: %s\r\n"
                        "Content-Type: application/octet-stream\r\n"
                        "X-Agyhook-Kind: tls-plaintext\r\nContent-Length: %lu\r\n"
                        "Connection: close\r\n\r\n",
                        (char *)g_fwd_host, (unsigned long)len);
    if (hn > 0) p_send(s, head, hn, 0);
    DWORD sent = 0;
    while (sent < len) {
        int n = p_send(s, (const char *)data + sent, (int)(len - sent), 0);
        if (n <= 0) break;
        sent += (DWORD)n;
    }
    char sink[512];                        /* drain enough to see the status */
    p_recv(s, sink, sizeof(sink), 0);
    p_closesocket(s);
    return 1;
}

static DWORD WINAPI forwarder_loop(LPVOID unused) {
    (void)unused;
    log_line("forwarder -> %s:%u\r\n", g_fwd_host, g_fwd_port);
    for (;;) {
        LONG tail = g_qtail, head = g_qhead;
        if (tail == head) { Sleep(5); continue; }
        QItem it = g_queue[tail % QSLOTS];
        InterlockedExchange(&g_qtail, tail + 1);
        if (it.data) {
            if (!forward_one(it.data, it.len)) {
                log_line("forward failed (len=%lu)\r\n", (unsigned long)it.len);
            }
            HeapFree(GetProcessHeap(), 0, it.data);
        }
    }
    return 0;
}

/* Called from the exception handler: copy and enqueue only. */
static void enqueue(const BYTE *data, DWORD len) {
    if (!g_fwd_on) return;
    LONG head = g_qhead;
    if (head - g_qtail >= QSLOTS) { InterlockedIncrement(&g_dropped); return; }
    BYTE *copy = (BYTE *)HeapAlloc(GetProcessHeap(), 0, len);
    if (!copy) return;
    memcpy(copy, data, len);
    g_queue[head % QSLOTS].data = copy;
    g_queue[head % QSLOTS].len  = len;
    MemoryBarrier();
    InterlockedExchange(&g_qhead, head + 1);
}

/* ------------------------------------------------------------------ VEH */

static LONG CALLBACK veh(PEXCEPTION_POINTERS ep) {
    CONTEXT *c = ep->ContextRecord;
    DWORD code = ep->ExceptionRecord->ExceptionCode;
    log_line("[veh] code=%#lx rip=%p\r\n", (unsigned long)code, (void *)c->Rip);

    if (code == EXCEPTION_BREAKPOINT &&
        (BYTE *)ep->ExceptionRecord->ExceptionAddress == g_target) {
        /* Go register ABI for func (c *Conn) Write(b []byte) (int, error):
         *   AX = receiver, BX = b.ptr, CX = b.len, DI = b.cap
         * Nothing has executed yet, so these are still live. Fall back to
         * scanning the GPRs if the shape does not hold. */
        BYTE *p = (BYTE *)c->Rbx;
        ULONG64 len = c->Rcx;
        int ok = (len >= 16 && len <= MAX_CAPTURE && readable(p, (size_t)len)
                  && looks_like_request(p, (DWORD)len));
        if (!ok && (len = 0, 1)) {
            ULONG64 regs[] = { c->Rax, c->Rbx, c->Rcx, c->Rdx, c->Rsi, c->Rdi,
                               c->R8,  c->R9,  c->R10, c->R11 };
            for (int i = 0; i < (int)(sizeof(regs) / sizeof(regs[0])); i++) {
                BYTE *q = (BYTE *)regs[i];
                if (!q) continue;
                for (ULONG64 cand = 64; cand <= MAX_CAPTURE; cand <<= 1) {
                    if (!readable(q, (size_t)cand)) break;
                    if (looks_like_request(q, (DWORD)cand)) { p = q; len = cand; ok = 1; break; }
                }
                if (ok) break;
            }
        }
        if (ok) {
            log_line("[hit %ld] tls.Write len=%lu\r\n",
                     InterlockedIncrement(&g_hits), (unsigned long)len);
            WriteFile(g_log, p, (DWORD)len, NULL, NULL);
            WriteFile(g_log, "\r\n----\r\n", 8, NULL, NULL);
            enqueue(p, (DWORD)len);
        } else {
            log_line("[hit %ld] tls.Write (not our request)\r\n",
                     InterlockedIncrement(&g_hits));
        }
done:
        /* Restore the real byte and let the instruction run. We deliberately do
         * NOT use the TF/single-step dance: RIP still points at g_target, so the
         * restored instruction executes on continue. A background thread
         * reinstalls the breakpoint shortly after. */
        remove_breakpoint();
        InterlockedExchange(&g_rearm, 1);
        return EXCEPTION_CONTINUE_EXECUTION;
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

/* ---------------------------------------------------------------- entry */

static DWORD WINAPI rearm_loop(LPVOID unused) {
    (void)unused;
    for (;;) {
        if (InterlockedExchange(&g_rearm, 0)) {
            Sleep(2);                     /* let the real instruction retire */
            install_breakpoint();
        }
        Sleep(1);
    }
    return 0;
}

static DWORD WINAPI worker(LPVOID unused) {
    (void)unused;

    char logpath[MAX_PATH];
    GetEnvironmentVariableA("AGYHOOK_LOG", logpath, MAX_PATH);
    if (!logpath[0]) lstrcpyA(logpath, "C:\\Windows\\Temp\\agyhook.log");
    g_log = CreateFileA(logpath, FILE_APPEND_DATA, FILE_SHARE_READ, NULL,
                        OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (g_log == INVALID_HANDLE_VALUE) return 1;

    HMODULE mod = GetModuleHandleA(NULL);
    g_target = (BYTE *)mod + TARGET_RVA;

    log_line("agyhook attached: base=%p target=%p\r\n", mod, g_target);

    /* Vectored handlers run LIFO. Registering here (during DLL_PROCESS_ATTACH)
     * puts us behind whatever the Go runtime registers once it initialises --
     * and Go then consumes our INT3 and panics. So register, let the runtime
     * settle, then re-register to move in front of it. */
    PVOID first = AddVectoredExceptionHandler(1, veh);
    Sleep(4000);
    if (first) RemoveVectoredExceptionHandler(first);
    AddVectoredExceptionHandler(1, veh);
    log_line("veh re-registered (runtime settled)\r\n");

    install_breakpoint();
    CreateThread(NULL, 0, rearm_loop, NULL, 0, NULL);
    log_line("breakpoint installed (byte=%#x)\r\n", *g_target);

    /* AGYHOOK_FORWARD="host:port" turns on plaintext forwarding. */
    char fwd[64];
    if (GetEnvironmentVariableA("AGYHOOK_FORWARD", fwd, sizeof(fwd)) > 0) {
        char *colon = fwd;
        while (*colon && *colon != ':') colon++;
        if (*colon == ':') {
            *colon = 0;
            unsigned port = 0;
            for (char *q = colon + 1; *q >= '0' && *q <= '9'; q++) port = port * 10 + (*q - '0');
            if (fwd[0] && port) {
                lstrcpynA(g_fwd_host, fwd, sizeof(g_fwd_host));
                g_fwd_port = (unsigned short)port;
                if (load_winsock()) {
                    char ws[64] = {0};
                    p_WSAStartup(0x0202, ws);
                    InterlockedExchange(&g_fwd_on, 1);
                    CreateThread(NULL, 0, forwarder_loop, NULL, 0, NULL);
                } else {
                    log_line("winsock unavailable; forwarding off\r\n");
                }
            }
        }
    }
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved) {
    (void)inst; (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(inst);
        CreateThread(NULL, 0, worker, NULL, 0, NULL);
    }
    return TRUE;
}
