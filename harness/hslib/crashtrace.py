"""This run's crash backtrace for a worldserver we launch ourselves (core.login_logout_race_baseline).

macOS ReportCrash is not a per-crash source: when the same binary crashed the same way again it wrote no new .ips,
only bumped the mtime of an older report, so a check reading it saw frames from a past crash (verifier, 2026-10-03:
6 of 6 runs read worldserver-2026-10-03-083748.ips). Instead, crashtrace.c is injected with DYLD_INSERT_LIBRARIES
(the baseline binary is ad-hoc linker-signed without the hardened runtime, so dyld honours it) and writes
`<dir>/crash-<pid>.txt` from inside the crashing process: signal, exception syndrome, main-image UUID and slide,
and the faulting thread's pc + frame-pointer chain. `read()` parses it and symbolizes the main-image frames with
`atos` against the binary file (no task port or debugger needed; lldb needs Developer Mode, which is off here).

The shim is compiled from source on demand into baseline/tmp/ (gitignored) with the Command Line Tools SDK."""
import struct
import subprocess
from pathlib import Path

SRC = Path(__file__).with_name("crashtrace.c")
ENV_DIR = "HS_CRASHTRACE_DIR"
# arm64 ESR_ELx exception classes (ESR bits 31:26) of a real bad memory access: data abort from EL0 / same EL.
DATA_ABORT_EC = (0x24, 0x25)


class CrashTraceError(RuntimeError):
    pass


def build(out: Path) -> Path:
    """Compile crashtrace.c to `out` (a dylib) unless it is newer than the source. Returns `out`."""
    if out.exists() and out.stat().st_mtime >= SRC.stat().st_mtime:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    sdk = subprocess.run(["xcrun", "--show-sdk-path"], capture_output=True, text=True).stdout.strip()
    res = subprocess.run(["clang", "-print-resource-dir"], capture_output=True, text=True).stdout.strip()
    # -nostdinc + explicit SDK paths: this Mac has a stray /usr/local/include that breaks the default search.
    cmd = ["clang", "-isysroot", sdk, "-nostdinc", "-isystem", f"{sdk}/usr/include", "-isystem", f"{res}/include",
           "-O1", "-fno-omit-frame-pointer", "-dynamiclib", "-o", str(out), str(SRC)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise CrashTraceError(f"building {SRC.name} failed: {r.stderr[-1500:]}")
    return out


def env(dylib: Path, outdir: Path) -> dict:
    """Environment additions that make a launched process record its fatal SIGSEGV/SIGBUS into `outdir`."""
    outdir.mkdir(parents=True, exist_ok=True)
    return {"DYLD_INSERT_LIBRARIES": str(dylib), ENV_DIR: str(outdir)}


def clear(outdir: Path) -> list[str]:
    """Remove earlier crash-*.txt records (ours, under baseline/tmp) so only this run's can be read."""
    gone = []
    for p in outdir.glob("crash-*.txt"):
        p.unlink()
        gone.append(p.name)
    return gone


def text_vmaddr(binary: Path) -> int:
    """vmaddr of the __TEXT segment of a thin 64-bit Mach-O."""
    data = binary.read_bytes()[:65536]
    magic, _, _, _, ncmds, _, _, _ = struct.unpack_from("<IiiIIIII", data, 0)
    if magic != 0xFEEDFACF:
        raise CrashTraceError(f"{binary} is not a thin 64-bit Mach-O")
    off = 32
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", data, off)
        if cmd == 0x19 and data[off + 8:off + 24].rstrip(b"\0") == b"__TEXT":     # LC_SEGMENT_64
            return struct.unpack_from("<Q", data, off + 24)[0]
        off += size
    raise CrashTraceError(f"no __TEXT segment in {binary}")


def parse(text: str) -> dict:
    rec = {"frames": [], "complete": text.rstrip().endswith("end")}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "frame":
            rec["frames"].append(int(parts[2], 16))
        elif len(parts) == 2 and parts[0] in ("pid", "signo", "code", "si_pid", "exc"):
            rec[parts[0]] = int(parts[1])
        elif len(parts) == 2 and parts[0] in ("addr", "slide", "esr", "far", "pc", "lr"):
            rec[parts[0]] = int(parts[1], 16)
        elif len(parts) == 2 and parts[0] == "uuid":
            rec["uuid"] = parts[1].lower()
    return rec


def symbolize(binary: Path, slide: int, addrs: list[int]) -> list[str]:
    """atos names for addresses; return addresses (all but the first) are looked up at addr-1, as debuggers do.
    Addresses outside the binary come back as hex."""
    if not addrs:
        return []
    look = [addrs[0]] + [a - 1 for a in addrs[1:]]
    load = text_vmaddr(binary) + slide
    r = subprocess.run(["atos", "-o", str(binary), "-arch", "arm64", "-l", hex(load), *map(hex, look)],
                       capture_output=True, text=True, timeout=120)
    names = r.stdout.splitlines()
    if r.returncode != 0 or len(names) != len(look):
        raise CrashTraceError(f"atos failed ({r.returncode}): {r.stderr[-800:]}")
    return [n[:300] for n in names]


def read(outdir: Path, pid: int, binary: Path) -> dict | None:
    """The record `pid` wrote (None if it wrote none), with symbolized frames and the fault classification."""
    p = outdir / f"crash-{pid}.txt"
    if not p.exists():
        return None
    rec = parse(p.read_text(errors="replace"))
    rec["file"] = str(p)
    ec = (rec.get("esr", 0) >> 26) & 0x3F
    rec["esr_exception_class"] = hex(ec)
    rec["hardware_fault"] = ec in DATA_ABORT_EC
    rec["top_frames"] = symbolize(binary, rec.get("slide", 0), rec["frames"][:16])
    rec["lr_symbol"] = symbolize(binary, rec.get("slide", 0), [rec["lr"]])[0] if "lr" in rec else None
    for k in ("addr", "slide", "esr", "far", "pc", "lr"):
        if k in rec:
            rec[k] = hex(rec[k])
    rec["frames"] = [hex(a) for a in rec["frames"]]
    return rec
