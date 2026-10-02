import argparse
import json
import sys
from pathlib import Path

from .util import log


def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["probe"]:                 # pass everything (incl. --help) through to the probe CLI
        from .probe.__main__ import main as probe_main
        return probe_main(argv[1:])
    ap = argparse.ArgumentParser(prog="hs", description="Hearthstone harness")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="sync pinned vendor trees and build/install the server")
    b.add_argument("--clean", action="store_true",
                   help="clean build: from-empty if the build inputs changed since the last passing from-empty build "
                        "(or no stamp), else prove the install is current (stamp + no-op incremental build)")
    b.add_argument("--force-clean", action="store_true", help="always wipe build/ and build from empty")
    b.add_argument("-j", "--jobs", type=int, default=8)

    u = sub.add_parser("up", help="start mysqld, authserver, worldserver; wait for readiness")
    u.add_argument("--world-datadir", type=Path, help="override worldserver DataDir (self-tests)")
    u.add_argument("--timeout", type=int)
    sub.add_parser("down", help="graceful shutdown of the whole stack")
    sub.add_parser("status", help="process/port/readiness status")

    g = sub.add_parser("gm", help="run a GM command over SOAP")
    g.add_argument("command", nargs="+")

    q = sub.add_parser("sql", help="run SQL against auth|world|characters|playerbots")
    q.add_argument("db", choices=["auth", "world", "characters", "playerbots"])
    q.add_argument("sql", nargs="+")

    r = sub.add_parser("run", help="run checks; write harness/results.json + summary.md")
    r.add_argument("--only", action="append", help="glob of check ids (repeatable)")
    r.add_argument("--leave-up", action="store_true", help="don't shut the stack down at the end")
    r.add_argument("--tier", choices=["quick", "full", "slow"], default="full",
                   help="quick: quick checks only; full (default, the merge gate): quick+full; slow: everything")
    r.add_argument("--force-clean", action="store_true", help="build.clean always does a from-empty build")
    r.add_argument("--results-dir", type=Path, help=argparse.SUPPRESS)   # nested runs (report.tiers) write here
    ls = sub.add_parser("list", help="list registered checks")
    ls.add_argument("--json", action="store_true", help="machine-readable: id, order, capability, stack, tier")

    pr = sub.add_parser("probe", help="headless 3.3.5a protocol probe (logon, realm list, world, char enum/login)",
                        add_help=False)
    pr.add_argument("probe_args", nargs=argparse.REMAINDER)
    fx = sub.add_parser("fixture", help="test fixtures: setup | level N | tele NAME | item ENTRY | quest ID")
    fx.add_argument("action", choices=["setup", "level", "tele", "item", "quest"])
    fx.add_argument("value", nargs="?")

    c = sub.add_parser("client-config", help="point a 3.3.5a client dir at localhost (no binary changes)")
    c.add_argument("client_dir", type=Path)

    ci = sub.add_parser("client-install",
                        help="install a 3.3.5a client (zip or folder, read-only) into client/ + manifest")
    ci.add_argument("source", type=Path)

    wi = sub.add_parser("wine-install", help="fetch + SHA-256-verify the pinned Wine runtime (fork.lock) into env/")
    wi.add_argument("--force", action="store_true", help="re-download even if the pinned runtime is present")

    gi = sub.add_parser("ghidra-install", help="fetch + SHA-256-verify the pinned Ghidra (fork.lock) into env/ (ADR-0005)")
    gi.add_argument("--force", action="store_true")
    rp = sub.add_parser("re", help="read-only Ghidra analysis of a COPY of a client binary (env/re/)")
    rp.add_argument("mode", choices=["import", "entry", "strings", "decompile", "xrefs"])
    rp.add_argument("arg", nargs="?", help="strings: text to find; decompile/xrefs: address (e.g. 0x00401000)")
    rp.add_argument("--name", default="Wow.exe", help="client binary to analyze (default Wow.exe)")
    rp.add_argument("--analyze", action="store_true", help="import: run full auto-analysis (slow; needed for decompile/xrefs)")

    la = sub.add_parser("launch", help="stack up, client-config, fixtures, start Wow.exe under Wine (ADR-0004)")
    la.add_argument("--smoke", action="store_true",
                    help="launch, require Wow.exe alive after 30 s, then stop it and every prefix process")
    la.add_argument("--smoke-seconds", type=int, default=30, help=argparse.SUPPRESS)

    a = ap.parse_args(argv)

    if a.cmd == "build":
        from .build import build, BuildError
        try:
            _print(build(clean=a.clean, jobs=a.jobs, force_clean=a.force_clean))
        except BuildError as e:
            log(f"BUILD FAILED: {e}")
            if e.info:
                _print(e.info)
            return 1
        return 0

    if a.cmd in ("up", "down", "status"):
        from . import stack
        try:
            if a.cmd == "up":
                _print(stack.up(world_datadir=a.world_datadir, timeout=a.timeout))
            elif a.cmd == "down":
                info = stack.down()
                _print(info)
                return 1 if info.get("remaining") else 0
            else:
                _print(stack.status())
        except stack.StackError as e:
            log(f"{a.cmd.upper()} FAILED: {e}")
            if e.log_excerpt:
                print("----- log excerpt -----\n" + e.log_excerpt, file=sys.stderr)
            return 1
        return 0

    if a.cmd == "gm":
        from .soap import gm
        try:
            res = gm(" ".join(a.command))
        except OSError as e:
            log(f"SOAP unreachable: {e}")
            return 2
        print(res.output.rstrip())
        return 0 if res.ok else 1

    if a.cmd == "sql":
        from .db import query, SQLError
        try:
            for row in query(" ".join(a.sql), a.db):
                print("\t".join(row))
        except SQLError as e:
            log(str(e))
            return 1
        return 0

    if a.cmd == "run":
        from . import paths as P
        from .runner import run
        if a.results_dir:
            a.results_dir.mkdir(parents=True, exist_ok=True)
            P.RESULTS, P.SUMMARY = a.results_dir.resolve() / "results.json", a.results_dir.resolve() / "summary.md"
        return run(only=a.only, leave_up=a.leave_up, tier=a.tier, force_clean=a.force_clean)

    if a.cmd == "list":
        from .registry import load_all
        checks = sorted(load_all().values(), key=lambda c: (c.order, c.id))
        if a.json:
            _print([{"id": c.id, "order": c.order, "capability": c.capability, "stack": c.stack, "tier": c.tier,
                     "tier_declared": c.tier_declared} for c in checks])
            return 0
        for c in checks:
            t = c.tier if c.tier_declared else f"{c.tier}(undeclared)"
            print(f"{c.order:4} {c.id:32} [{c.capability}] stack={c.stack} tier={t}  {c.doc.splitlines()[0] if c.doc else ''}")
        return 0

    if a.cmd == "probe":
        from .probe.__main__ import main as probe_main
        return probe_main(a.probe_args)

    if a.cmd == "fixture":
        from . import fixtures
        try:
            _print(fixtures.cli(a.action, a.value))
        except fixtures.FixtureError as e:
            log(f"FIXTURE FAILED: {e}")
            _print(e.evidence)
            return 1
        return 0

    if a.cmd == "client-config":
        try:
            from .clientcfg import configure
        except ImportError:
            log("client-config not implemented yet (task 6.1)")
            return 2
        from .clientcfg import ClientConfigError
        try:
            _print(configure(a.client_dir))
        except ClientConfigError as e:
            log(f"CLIENT-CONFIG FAILED: {e}")
            return 1
        return 0

    if a.cmd == "client-install":
        from .clientinst import install, ClientInstallError
        from .clientcfg import ClientConfigError
        try:
            res = install(a.source)
        except (ClientInstallError, ClientConfigError) as e:
            log(f"CLIENT-INSTALL FAILED: {e}")
            return 1
        _print(res)
        return 0 if res.get("version_ok", True) else 1

    if a.cmd == "wine-install":
        from .wineinst import install as wine_install, WineInstallError
        try:
            _print(wine_install(force=a.force))
        except WineInstallError as e:
            log(f"WINE-INSTALL FAILED: {e}")
            return 1
        return 0

    if a.cmd in ("ghidra-install", "re"):
        from . import ghidra
        try:
            if a.cmd == "ghidra-install":
                _print(ghidra.install(force=a.force))
            elif a.mode == "import":
                _print(ghidra.analyze(a.name) if a.analyze else ghidra.run_report("entry", name=a.name, fresh_import=True))
            else:
                mode = {"strings": "findascii"}.get(a.mode, a.mode)
                if a.mode in ("strings", "decompile", "xrefs") and not a.arg:
                    log(f"`hs re {a.mode}` needs an argument")
                    return 2
                res = ghidra.run_report(mode, a.arg, name=a.name)
                print("\n".join(res["lines"]))
        except ghidra.GhidraError as e:
            log(f"RE FAILED: {e}")
            return 1
        return 0

    if a.cmd == "launch":
        from . import launch
        try:
            res = launch.run(smoke=a.smoke, smoke_seconds=a.smoke_seconds)
        except launch.LaunchError as e:
            log(f"LAUNCH FAILED: {e}")
            _print(e.evidence)
            return 1
        _print(res)
        return 0
    return 2
