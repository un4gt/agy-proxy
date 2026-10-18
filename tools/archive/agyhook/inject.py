"""Launch agy.exe suspended and inject agyhook.dll before it can connect.

    python tools/agyhook/inject.py --dll build/agyhook.dll --log C:\\Temp\\agy.log -- agy.exe -p "hi"

Creates the process suspended, remote-LoadLibrary's the DLL, then resumes, so
the crypto/tls.(*Conn).Write breakpoint is in place before the first handshake.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

CREATE_SUSPENDED = 0x00000004
INFINITE = 0xFFFFFFFF
MEM_COMMIT_RESERVE = 0x3000
PAGE_READWRITE = 0x04

k32.CreateProcessA.argtypes = [
    wintypes.LPCSTR, wintypes.LPSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCSTR,
    ctypes.c_void_p, ctypes.c_void_p,
]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                               wintypes.DWORD, wintypes.DWORD]
k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.CreateRemoteThread.restype = wintypes.HANDLE
k32.CreateRemoteThread.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.c_void_p]
k32.GetModuleHandleA.restype = ctypes.c_void_p
k32.GetModuleHandleA.argtypes = [wintypes.LPCSTR]
k32.GetProcAddress.restype = ctypes.c_void_p
k32.GetProcAddress.argtypes = [ctypes.c_void_p, wintypes.LPCSTR]
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.ResumeThread.argtypes = [wintypes.HANDLE]


class STARTUPINFOA(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPSTR),
                ("lpDesktop", wintypes.LPSTR), ("lpTitle", wintypes.LPSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def inject(pid_handle, dll_path: str) -> None:
    kernel = k32.GetModuleHandleA(b"kernel32.dll")
    load_lib = k32.GetProcAddress(kernel, b"LoadLibraryA")
    if not load_lib:
        sys.exit("LoadLibraryA not found")

    payload = dll_path.encode("ascii") + b"\x00"
    remote = k32.VirtualAllocEx(pid_handle, None, len(payload),
                                MEM_COMMIT_RESERVE, PAGE_READWRITE)
    if not remote:
        sys.exit(f"VirtualAllocEx failed: {ctypes.get_last_error()}")
    written = ctypes.c_size_t(0)
    if not k32.WriteProcessMemory(pid_handle, remote, payload, len(payload),
                                  ctypes.byref(written)):
        sys.exit(f"WriteProcessMemory failed: {ctypes.get_last_error()}")

    thread = k32.CreateRemoteThread(pid_handle, None, 0, load_lib, remote, 0, None)
    if not thread:
        sys.exit(f"CreateRemoteThread failed: {ctypes.get_last_error()}")
    k32.WaitForSingleObject(thread, 10000)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", required=True)
    ap.add_argument("--log", default="")
    ap.add_argument("--cwd", default="")
    ap.add_argument("command", nargs=argparse.REMAINDER,
                    help="-- agy.exe [args...]")
    args = ap.parse_args()

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        ap.error("no command given")

    si = STARTUPINFOA()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()

    if args.log:
        import os
        os.environ["AGYHOOK_LOG"] = args.log

    # CreateProcessA takes a mutable command line
    cmdline = ctypes.create_string_buffer(" ".join(cmd).encode("ascii"))
    ok = k32.CreateProcessA(
        None, cmdline, None, None, False, CREATE_SUSPENDED,
        None, args.cwd.encode("ascii") if args.cwd else None,
        ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        sys.exit(f"CreateProcessA failed: {ctypes.get_last_error()}")

    print(f"pid={pi.dwProcessId} (suspended)")
    try:
        inject(pi.hProcess, args.dll)
        print(f"injected {args.dll}")
    except SystemExit as exc:
        k32.TerminateProcess(pi.hProcess, 1)
        raise
    k32.ResumeThread(pi.hThread)
    print("resumed")
    k32.WaitForSingleObject(pi.hProcess, INFINITE)
    code = wintypes.DWORD(0)
    k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
    print(f"exited: {code.value}")
    return code.value


if __name__ == "__main__":
    sys.exit(main())
