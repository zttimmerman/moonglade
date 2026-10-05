"""Process table without `ps` (stdlib ctypes over libproc + sysctl).

Why: /bin/ps is setuid root, and macOS refuses to exec a setuid binary from a sandboxed process (sandbox-exec
fails with EPERM even under `(allow default)`). `play.offline` runs the whole play loop under sandbox-exec, so
process discovery must not depend on ps. This reads the same kernel data ps does: KERN_PROCARGS2 for the
argument vector + environment (only readable for this user's processes, like `ps -x`), kinfo_proc for the
state. A process whose arguments can't be read is shown as `(name)`, as ps does.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)

CTL_KERN, KERN_ARGMAX, KERN_PROC, KERN_PROC_PID, KERN_PROCARGS2 = 1, 8, 14, 1, 49
SZOMB = 5
# struct kinfo_proc (arm64/x86_64): kp_proc.p_stat is a char at offset 36 (p_un 16 + p_vmspace 8 +
# p_sigacts 8 + p_flag 4); kp_eproc.e_ucred.cr_uid sits at a fixed offset too but we read the uid via
# proc_pidinfo instead.
_KINFO_SIZE = 648
_P_STAT_OFF = 36


def _sysctl(mib: list[int], size: int) -> bytes | None:
    arr = (ctypes.c_int * len(mib))(*mib)
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(size)
    if _libc.sysctl(arr, len(mib), buf, ctypes.byref(n), None, ctypes.c_size_t(0)) != 0:
        return None
    return buf.raw[: n.value]


_ARGMAX = struct.unpack("i", _sysctl([CTL_KERN, KERN_ARGMAX], 4) or struct.pack("i", 1 << 20))[0]


def pids() -> list[int]:
    n = _libproc.proc_listallpids(None, 0)
    while True:
        cap = n + 64
        buf = (ctypes.c_int * cap)()
        got = _libproc.proc_listallpids(buf, ctypes.sizeof(buf))
        if got < cap:
            return [p for p in buf[:got] if p > 0]
        n = got


def name(pid: int) -> str:
    buf = ctypes.create_string_buffer(256)
    _libproc.proc_name(pid, buf, 256)
    return buf.value.decode(errors="replace")


def uid(pid: int) -> int | None:
    # struct proc_bsdinfo: pbi_flags, pbi_status, pbi_xstatus, pbi_pid, pbi_ppid, pbi_uid, ... (uint32 each)
    buf = ctypes.create_string_buffer(136)
    if _libproc.proc_pidinfo(pid, 3, ctypes.c_uint64(0), buf, 136) < 136:   # PROC_PIDTBSDINFO
        return None
    return struct.unpack_from("I", buf.raw, 20)[0]


def ppid(pid: int) -> int | None:
    """Parent pid (proc_bsdinfo.pbi_ppid), None if the process is gone."""
    buf = ctypes.create_string_buffer(136)
    if _libproc.proc_pidinfo(pid, 3, ctypes.c_uint64(0), buf, 136) < 136:   # PROC_PIDTBSDINFO
        return None
    return struct.unpack_from("I", buf.raw, 16)[0]


def path(pid: int) -> str | None:
    """The executable's path (proc_pidpath), None if the process is gone or unreadable. Unlike argv it survives a
    program rewriting its arguments (Wine shows Windows paths such as C:\\windows\\system32\\notepad.exe)."""
    buf = ctypes.create_string_buffer(4096)
    n = _libproc.proc_pidpath(pid, buf, 4096)
    return buf.value.decode(errors="replace") if n > 0 else None


def is_zombie(pid: int) -> bool:
    raw = _sysctl([CTL_KERN, KERN_PROC, KERN_PROC_PID, pid], _KINFO_SIZE)
    return bool(raw) and len(raw) > _P_STAT_OFF and raw[_P_STAT_OFF] == SZOMB


def args_env(pid: int) -> tuple[list[str], list[str]] | None:
    """(argv, environment) from KERN_PROCARGS2, parsed the way ps does; None if unreadable."""
    raw = _sysctl([CTL_KERN, KERN_PROCARGS2, pid], _ARGMAX)
    if not raw or len(raw) < 4:
        return None
    argc = struct.unpack_from("i", raw, 0)[0]
    p = raw.find(b"\0", 4)                 # end of the exec path
    if p < 0:
        return None
    while p < len(raw) and raw[p] == 0:    # padding after the exec path
        p += 1
    strings = raw[p:].split(b"\0")
    argv = [s.decode(errors="replace").rstrip() for s in strings[:argc]]
    env = []
    for s in strings[argc:]:
        if not s:
            break
        env.append(s.decode(errors="replace"))
    return argv, env


def table(with_env: bool = False, own_only: bool = False) -> list[tuple[int, str]]:
    """[(pid, command)] like `ps -axo pid=,command=` (or `ps -E -x -ww`: command + environment, own processes)."""
    me = os.getuid()
    rows = []
    for pid in pids():
        if own_only and uid(pid) != me:
            continue
        ae = args_env(pid)
        if ae is None or not ae[0]:
            cmd = f"({name(pid)})"
        else:
            cmd = " ".join(ae[0]).rstrip()
            if with_env and ae[1]:
                cmd += " " + " ".join(ae[1])
        rows.append((pid, cmd))
    return sorted(rows)
