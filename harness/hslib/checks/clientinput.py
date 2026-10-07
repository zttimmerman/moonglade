"""One hardware key event (Shift down + up) for the real client, from inside its own Wine prefix (easy-setup 1.9).

Why: the 3.3.5a client logs the character out on its own once it has seen no hardware input for 30 minutes. It
measures that as `now - last hardware input`, where `now` is milliseconds since the Mac booted (GetTickCount family)
and `last hardware input` starts at 0 and is only set by the client's keyboard/mouse event handlers. Read-only
analysis of Wow.exe 12340 (sha256 aa63a575...; ADR-0005, llvm-objdump of the env/re copy): the idle check at
0x0052b24c calls 0x0086ae20 (ms clock), subtracts the global 0x00b499a4 (zero-initialised .data; written only at
0x004943e2..0x004954ad, the input event handlers), compares signed against 300000 and 1800000 ms, then raises
IDLE_MESSAGE and calls the logout routine 0x006b1930 (the routine Lua Logout() reaches through 0x00510430).
A harness run drives the client only through the GlueXML test hook (Lua clicks, no hardware input), so the "idle"
time is the Mac's uptime: from 30 minutes after boot until the 32-bit millisecond count turns negative
(24.8 days) the character leaves the world 0.07-0.4 s after entering it (CMSG_LOGOUT_REQUEST from the client, then
the test account's instant logout). Measured 2026-10-07: uptime 37.8 days before the 05:02 power loss (negative
count: no logout, checks passed), 2-7 h after it (every world entry logged out); with this one Shift press sent
after the client came up, the character stayed and the check's own logout ran.

What: a 1 KB PE32 console program written here byte by byte (no Windows toolchain needed), which calls
user32!SendInput with two keyboard INPUTs (VK_SHIFT down, up) and exits with SendInput's return value (2 = both
injected). Run with the prefix's own wine while the client runs; Wine queues the events to the client's window as
hardware input. Shift alone does nothing in the glue screens or the world. Test runs only (seamless play session);
never in a player's ./play.
"""
from __future__ import annotations

import struct
import subprocess
from pathlib import Path

from .. import launch

BASE, SECT_RVA, SECT_RAW, ALIGN = 0x400000, 0x1000, 0x200, 0x200
VK_SHIFT, SCAN_SHIFT, KEYEVENTF_KEYUP, INPUT_KEYBOARD = 0x10, 0x2A, 0x2, 1


def _section() -> bytes:
    s = bytearray(ALIGN)
    va = lambda off: BASE + SECT_RVA + off                         # noqa: E731
    o_inputs, o_desc, o_u_ilt, o_u_iat, o_k_ilt, o_k_iat = 0x40, 0x80, 0xC0, 0xC8, 0xD0, 0xD8
    o_send, o_exit, o_user32, o_kernel32 = 0xE0, 0xF0, 0x100, 0x110
    code = (b"\x6a\x1c" + b"\x68" + struct.pack("<I", va(o_inputs)) + b"\x6a\x02"     # SendInput(2, inputs, 28)
            + b"\xff\x15" + struct.pack("<I", va(o_u_iat))
            + b"\x50" + b"\xff\x15" + struct.pack("<I", va(o_k_iat)))                 # ExitProcess(eax)
    s[0:len(code)] = code
    for i, flags in enumerate((0, KEYEVENTF_KEYUP)):                                   # INPUT, 28 bytes each
        struct.pack_into("<IHHIII8x", s, o_inputs + 28 * i, INPUT_KEYBOARD, VK_SHIFT, SCAN_SHIFT, flags, 0, 0)
    rva = lambda off: SECT_RVA + off                               # noqa: E731
    struct.pack_into("<IIIII", s, o_desc, rva(o_u_ilt), 0, 0, rva(o_user32), rva(o_u_iat))
    struct.pack_into("<IIIII", s, o_desc + 20, rva(o_k_ilt), 0, 0, rva(o_kernel32), rva(o_k_iat))
    for ilt, iat, name in ((o_u_ilt, o_u_iat, o_send), (o_k_ilt, o_k_iat, o_exit)):
        struct.pack_into("<II", s, ilt, rva(name), 0)
        struct.pack_into("<II", s, iat, rva(name), 0)
    for off, txt in ((o_send, b"SendInput"), (o_exit, b"ExitProcess")):
        s[off + 2:off + 2 + len(txt)] = txt                       # hint 0, name, NUL
    s[o_user32:o_user32 + 10] = b"user32.dll"
    s[o_kernel32:o_kernel32 + 12] = b"kernel32.dll"
    return bytes(s)


def program() -> bytes:
    sect = _section()
    dos = bytearray(0x40)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)
    coff = struct.pack("<4sHHIIIHH", b"PE\0\0", 0x14C, 1, 0, 0, 0, 0xE0, 0x0103)
    dirs = [(0, 0)] * 16
    dirs[1] = (SECT_RVA + 0x80, 60)                                # import directory
    opt = struct.pack("<HBBIIIIIIIIIHHHHHHIIIIHHIIIIII", 0x10B, 1, 0, ALIGN, 0, 0, SECT_RVA, SECT_RVA, SECT_RVA,
                      BASE, 0x1000, ALIGN, 4, 0, 0, 0, 4, 0, 0, SECT_RVA + 0x1000, SECT_RAW, 0, 3, 0,
                      0x100000, 0x1000, 0x100000, 0x1000, 0, 16)
    opt += b"".join(struct.pack("<II", *d) for d in dirs)
    sec = struct.pack("<8sIIIIIIHHI", b".text", ALIGN, SECT_RVA, ALIGN, SECT_RAW, 0, 0, 0, 0, 0xE0000060)
    head = bytes(dos) + coff + opt + sec
    return head + b"\0" * (SECT_RAW - len(head)) + sect


def press_shift(tmp: Path, timeout: float = 60) -> dict:
    """Write the program into `tmp` and run it in the client's prefix. Returns evidence; injected == 2 on success."""
    exe = Path(tmp) / "hs-shift.exe"
    exe.write_bytes(program())
    r = subprocess.run([str(launch.WINE), str(exe)], env=launch.wine_env(), capture_output=True, text=True,
                       timeout=timeout)
    return {"exe": exe.name, "exit_code": r.returncode, "injected": r.returncode if r.returncode in (0, 1, 2) else None,
            "stderr_tail": r.stderr[-400:]}
