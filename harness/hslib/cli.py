import argparse
import json
import sys
from pathlib import Path

from .util import log


def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    from . import paths as _P0
    # release-first-launch's test switches: refused before anything unless the test flag is given in a live harness
    # run (the flag may appear anywhere on the command line and is taken out before parsing)
    _why = _P0.test_release_refusal(argv=argv)
    if _why:
        log(_why)
        return 2
    argv = [a for a in argv if a != _P0.RELEASE_TEST_FLAG]
    if argv[:1] == ["probe"]:                 # pass everything (incl. --help) through to the probe CLI
        from .probe.__main__ import main as probe_main
        return probe_main(argv[1:])
    ap = argparse.ArgumentParser(prog="hs", description="Moonglade harness")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="sync pinned vendor trees and build/install the server")
    b.add_argument("--clean", action="store_true",
                   help="clean build: from-empty if the build inputs changed since the last passing from-empty build "
                        "(or no stamp), else prove the install is current (stamp + no-op incremental build)")
    b.add_argument("--force-clean", action="store_true", help="always wipe build/ and build from empty")
    b.add_argument("-j", "--jobs", type=int, default=None,
                   help="compile jobs (default: this Mac's performance cores, never all cores; power-guard)")
    b.add_argument("--ignore-power", action="store_true",
                   help="don't pause the build on low battery (the guard pauses below 35 %% until 50 %%)")

    u = sub.add_parser("up", help="start mysqld, authserver, worldserver; wait for readiness")
    u.add_argument("--world-datadir", type=Path, help="override worldserver DataDir (self-tests)")
    u.add_argument("--timeout", type=int)
    u.add_argument("--profile", help="config profile (harness/hslib/config.py PROFILES: m0 | population | wotlk); "
                                     "default $HS_PROFILE or m0. A worldserver already running is left as is")
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
    r.add_argument("--tier", choices=["quick", "full", "slow", "gui", "backups"], default="full",
                   help="quick: quick checks only; full (default, the merge gate): quick+full; slow: everything; "
                        "gui: only the checks that need exclusive screen focus (full runs include them only when "
                        "a GUI path changed); backups: only the save-backups end-to-end checks (full runs include "
                        "them only when a backup path changed)")
    r.add_argument("--force-clean", action="store_true", help="build.clean always does a from-empty build")
    r.add_argument("--ignore-gui-preflight", action="store_true",
                   help="start an unfiltered full/slow/gui run although the screen is locked, the display asleep or "
                        "the battery low (GUI checks will then fail; for runs that knowingly accept that)")
    r.add_argument("--ignore-power", action="store_true",
                   help="full/slow/gui runs: don't pause between checks on low battery (below 35 %% until 50 %%)")
    r.add_argument("--results-dir", type=Path, help=argparse.SUPPRESS)   # nested runs (report.tiers) write here
    r.add_argument("--lanes", type=int, choices=[1, 2],
                   help="1: every check in lane A, serially; 2: the LANE_B checks in the second test stack alongside "
                        "(when memory and power allow). Default: 2 for unfiltered full/slow runs, else 1")
    ps_ = sub.add_parser("pop-session", help="one M2 population session (cold start with the population profile, "
                                            "warm-up, 10 min of samples); tuning runs for the budget record")
    ps_.add_argument("--n", type=int, help="random bots (default: config.POPULATION_N)")
    ps_.add_argument("--leave-up", action="store_true")
    wsz = sub.add_parser("world-size", help="show or set how many AI players ./play starts "
                                            "(small | normal | large | a number); stored in env/settings.json")
    wsz.add_argument("value", nargs="?")
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

    cl = sub.add_parser("client-lock", help="write-protect the installed client/ (chmod a-w + chflags uchg; "
                                            "client-install does this itself); --status only reports")
    cl.add_argument("--status", action="store_true", help="report the protection, change nothing")

    wi = sub.add_parser("wine-install", help="fetch + SHA-256-verify the pinned Wine runtime (fork.lock) into env/")
    wi.add_argument("--force", action="store_true", help="re-download even if the pinned runtime is present")

    gi = sub.add_parser("ghidra-install", help="fetch + SHA-256-verify the pinned Ghidra (fork.lock) into env/ (ADR-0005)")
    gi.add_argument("--force", action="store_true")
    rp = sub.add_parser("re", help="read-only Ghidra analysis of a COPY of a client binary (env/re/)")
    rp.add_argument("mode", choices=["import", "entry", "strings", "decompile", "xrefs"])
    rp.add_argument("arg", nargs="?", help="strings: text to find; decompile/xrefs: address (e.g. 0x00401000)")
    rp.add_argument("--name", default="Wow.exe", help="client binary to analyze (default Wow.exe)")
    rp.add_argument("--analyze", action="store_true", help="import: run full auto-analysis (slow; needed for decompile/xrefs)")

    mp = sub.add_parser("mpq", help="MPQ access: list files, extract one, write a test archive (never under client/)")
    mps = mp.add_subparsers(dest="mpq_cmd", required=True)
    ml = mps.add_parser("list", help="list files in an archive (from its (listfile))")
    ml.add_argument("archive", help="path, or relative to client/ (e.g. Data/common.MPQ)")
    ml.add_argument("pattern", nargs="?", help="case-insensitive substring filter")
    ml.add_argument("--sizes", action="store_true", help="also print size, compressed size and flags")
    me = mps.add_parser("extract", help="extract one file to a destination outside client/")
    me.add_argument("archive")
    me.add_argument("inside", help="path inside the archive, e.g. Interface\\GlueXML\\GlueXML.toc")
    me.add_argument("dest", type=Path, help="file path, or an existing directory")
    mw = mps.add_parser("write", help="write a NEW minimal v1 archive (test patches; refuses paths under client/), "
                                      "then verify it by reading it back")
    mw.add_argument("dest", type=Path, help="archive to create (outside client/)")
    mw.add_argument("files", nargs="+", metavar="INSIDE=SRC", help="internal name = local source file")
    mw.add_argument("--no-compress", action="store_true", help="store sectors uncompressed")

    la = sub.add_parser("launch", help="stack up, client-config, fixtures, start Wow.exe under Wine (ADR-0004)")
    la.add_argument("--smoke", action="store_true",
                    help="launch, require Wow.exe alive after 30 s, then stop it and every prefix process")
    la.add_argument("--smoke-seconds", type=int, default=30, help=argparse.SUPPRESS)

    cb = sub.add_parser("client-build", help="build the play client env/client-play/ from client/ + patches/client/ "
                                             "(ADR-0009; client/ is never written)")
    cb.add_argument("--skip", action="append", metavar="ID", help="leave a patch out of the queue (e.g. 0001); revert")
    cb.add_argument("--dest", type=Path, help=argparse.SUPPRESS)          # test-only: scratch play client
    cb.add_argument("--force", action="store_true", help="rebuild even if the stamp says it's up to date")
    cm = sub.add_parser("client-mode", help="show or set which client ./play and the app run: patched (the play "
                                            "client, default) or stock (the untouched client/)")
    cm.add_argument("value", nargs="?", choices=["patched", "stock"])

    dp = sub.add_parser("dock-policy", help="show or set how the game shows in the Dock when run from the app: "
                                            "accessory (one Moonglade icon, default), regular (the game keeps "
                                            "its own Moonglade-labelled icon) or off (no shim; ADR-0010)")
    dp.add_argument("value", nargs="?", choices=["accessory", "regular", "off"])

    bs = sub.add_parser("bootstrap", help="set up from a fresh clone: check this Mac, Homebrew packages (Brewfile), "
                                          "build the server, server data, databases, Wine, your client, the play "
                                          "client and the app; skips what's done (./bootstrap wraps this)")
    bs.add_argument("client", nargs="?", help="your WoW 3.3.5a (12340) enUS client: a .zip or a folder (optional "
                                               "once client/ is installed)")
    bs.add_argument("--yes", action="store_true", help="install missing Homebrew packages without asking")
    bs.add_argument("--check", action="store_true", help="only check this Mac and the client; change nothing")
    bs.add_argument("--jobs", type=int, default=None,
                    help="server build jobs (default: this Mac's performance cores)")
    bs.add_argument("--ignore-power", action="store_true",
                    help="don't pause setup on low battery (the guard pauses below 35 %% until 50 %%)")
    bs.add_argument("--allow-test-inject", action="store_true", help=argparse.SUPPRESS)   # setup.preflight only
    dl = sub.add_parser("download", help="fetch one pinned, SHA-256-verified download (fork.lock); client-data is "
                                         "also unpacked into data/client-data/")
    dl.add_argument("name", choices=["client-data", "vmangos", "wine-runtime", "ghidra"])
    sub.add_parser("vanilla-mode", help="show whether vanilla mode (the era profile) is on for ./play's world")

    sub.add_parser("app", help="build dist/Moonglade.app (double-clickable launcher for ./play; launcher-app) "
                               "and the Wine host shim env/dockshim/hsdock.dylib (ADR-0010)")
    tb = sub.add_parser("timing-baseline", help="per-check timing baseline (harness/timing-baseline.json): "
                        "`join <results.json> --label <change>` adds the checks a merging change's verifier full run "
                        "executed that the baseline lacks (existing times never change)")
    tb.add_argument("action", choices=["join"])
    tb.add_argument("results", nargs="?", help="a full run's results.json (default: harness/out/last-full-run/"
                                               "results.json, the last unfiltered full run)")
    tb.add_argument("--label", required=True)
    tb.add_argument("--baseline", type=Path, help=argparse.SUPPRESS)      # report.last_full_run: a scratch baseline
    ka = sub.add_parser("keep-awake", help="keep this Mac and its display awake (the assertion every `hs run` holds) "
                                           "for N hours (default 8) or until stopped (Ctrl-C)")
    ka.add_argument("--hours", type=float, default=8.0)
    ng = sub.add_parser("nightly", help="flake hunt: 3 unfiltered full runs on a clean main, results and a flake table "
                                        "in harness/out/nightly/<date>/; --install/--uninstall manage a user "
                                        "LaunchAgent that runs it nightly (installing it is Zach's call)")
    ngx = ng.add_mutually_exclusive_group()
    ngx.add_argument("--install", action="store_true", help="write and load ~/Library/LaunchAgents/<label>.plist")
    ngx.add_argument("--uninstall", action="store_true", help="unload and remove that LaunchAgent")
    ng.add_argument("--runs", type=int, default=3, help=argparse.SUPPRESS)
    ln = sub.add_parser("lane", help="the harness's second test stack, lane B (ADR-0011): init (template datadir + "
                                      "Wine prefix clone) | status | down | reset (fresh datadir clone); init and "
                                      "reset only inside a harness run")
    ln.add_argument("action", choices=["init", "status", "down", "reset"])
    ln.add_argument("--force", action="store_true", help="init: rebuild the template even if one exists")
    ic = sub.add_parser("icon", help="regenerate branding/AppIcon.png + .icns from env/branding/logo-source.jpeg")
    ic.add_argument("--out-dir", type=Path, help="write here instead of branding/ (e.g. a reproducibility check)")

    a = ap.parse_args(argv)

    import os as _os
    from . import paths as _P
    # second-stack: lane B (HS_LANE=b) only inside a harness run, refused before anything starts. `hs lane status`
    # and `hs lane down` only look at / stop lane B, so they're allowed anywhere (orphan healing, cleanup).
    if not (a.cmd == "lane" and a.action in ("status", "down")):
        _why = _P.lane_refusal()
        if _why:
            log(_why)
            return 2
    for _var in ("HS_TEST_PRISTINE_CLIENT", "HS_TEST_PRISTINE_MUTATE"):   # report.client_pristine's self-test only
        if _os.environ.get(_var) and (a.cmd != "run" or _os.environ.get("HS_HARNESS_RUN") != "1"):
            log(f"${_var} is a test switch of report.client_pristine's self-test (nested `hs run` only); unset it")
            return 2
    from .guienv import PREFLIGHT_TEST_ENV
    for _var in PREFLIGHT_TEST_ENV:                                      # report.gui_preflight's nested runs only
        if _os.environ.get(_var) and (a.cmd != "run" or _os.environ.get("HS_HARNESS_RUN") != "1"):
            log(f"${_var} is a test switch of report.gui_preflight (nested `hs run` only); unset it")
            return 2

    from . import lanes as _lanes                                        # second-stack's scheduler test switches
    _why = _lanes.refusal(a.cmd)
    if _why:
        log(_why)
        return 2

    from . import power                                                 # power-guard's test switches
    _why = power.refusal(a.cmd, getattr(a, "allow_test_inject", True) if a.cmd == "bootstrap" else True)
    if _why:
        log(_why)
        return 2

    if a.cmd == "build":
        from .build import build, plan_only, BuildError
        if a.jobs is not None and a.jobs < 1:
            log(f"--jobs must be 1 or more (got {a.jobs}); leave it out to use this Mac's performance cores")
            return 2
        try:
            pdir = power.plan_dir()
            if pdir:
                _print(plan_only(a.jobs, a.ignore_power, pdir))
            else:
                _print(build(clean=a.clean, jobs=a.jobs, force_clean=a.force_clean, ignore_power=a.ignore_power))
        except power.PowerStop as e:
            log("BUILD STOPPED by the power guard (see the line above)")
            _print(getattr(e, "info", None) or {"power": getattr(e, "guard", e.record)})
            return power.STOP_EXIT
        except power.JobsError as e:
            log(str(e))
            return 2
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
                _print(stack.up(world_datadir=a.world_datadir, timeout=a.timeout, profile=a.profile))
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
        from .runner import preflight_from_env
        pf = preflight_from_env(a.tier, a.only, a.ignore_gui_preflight)
        if pf["refuse"]:
            print(pf["refuse"])
            return 2
        if _os.environ.get("HS_TEST_PREFLIGHT_ONLY") == "1":                # report.gui_preflight: decision only
            _print({"preflight": pf})
            return 0
        return run(only=a.only, leave_up=a.leave_up, tier=a.tier, force_clean=a.force_clean, preflight=pf,
                   ignore_power=a.ignore_power, lanes=a.lanes)

    if a.cmd == "keep-awake":
        from .util import keep_awake_cli
        return keep_awake_cli(a.hours)

    if a.cmd == "nightly":
        from . import nightly
        return nightly.cli(do_install=a.install, do_uninstall=a.uninstall, runs=a.runs)

    if a.cmd == "pop-session":
        from . import population
        try:
            _print(population.cli(a.n, a.leave_up))
        except population.PopulationError as e:
            log(f"POP-SESSION FAILED: {e}")
            _print({k: v for k, v in e.evidence.items() if k not in ("samples",)})
            return 1
        return 0

    if a.cmd == "world-size":
        from . import config, settings
        try:
            _print((settings.set_world_size(a.value) if a.value else settings.world_size())
                   | {"presets": config.WORLD_SIZES})
        except settings.SettingsError as e:
            log(str(e))
            return 1
        return 0

    if a.cmd == "list":
        from .registry import load_all
        checks = sorted(load_all().values(), key=lambda c: (c.order, c.id))
        if a.json:
            _print([{"id": c.id, "order": c.order, "capability": c.capability, "stack": c.stack, "tier": c.tier,
                     "tier_declared": c.tier_declared, "gui": c.gui, "backups": c.backups} for c in checks])
            return 0
        for c in checks:
            t = c.tier if c.tier_declared else f"{c.tier}(undeclared)"
            print(f"{c.order:4} {c.id:32} [{c.capability}] stack={c.stack} tier={t}{' gui' if c.gui else ''}{' backups' if c.backups else ''}  "
                  f"{c.doc.splitlines()[0] if c.doc else ''}")
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

    if a.cmd == "client-lock":
        from . import pristine, paths as Pc
        if not Pc.CLIENT.is_dir():
            log("no client/ installed; run `harness/hs client-install <zip-or-folder>`")
            return 1
        res = {} if a.status else {"lock": pristine.lock(Pc.CLIENT)}
        st = pristine.status(Pc.CLIENT)
        _print(res | {"client": str(Pc.CLIENT), "status": st})
        return 0 if st["locked"] else 1

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

    if a.cmd == "mpq":
        from . import mpq
        try:
            if a.mpq_cmd == "list":
                names = mpq.list_archive(a.archive, a.pattern)
                if a.sizes:
                    with mpq.Archive(mpq.resolve_archive(a.archive)) as arc:
                        for n in names:
                            i = arc.info(n)
                            print(f"{i['size']}\t{i['csize']}\t{i['flags']}\t{n}")
                else:
                    print("\n".join(names))
                log(f"{len(names)} entries")
            elif a.mpq_cmd == "write":
                files = {}
                for spec in a.files:
                    inside, sep, src = spec.partition("=")
                    if not sep or not inside or not src:
                        raise mpq.MPQError(f"expected INSIDE=SRC, got {spec!r}")
                    files[inside] = Path(src).read_bytes()
                res = mpq.write_archive(a.dest, files, compress=not a.no_compress)
                res.update(mpq.verify_written(a.dest, files))
                _print(res)
            else:
                _print(mpq.extract(a.archive, a.inside, a.dest))
        except mpq.MPQError as e:
            log(f"MPQ FAILED: {e}")
            return 1
        except BrokenPipeError:                       # e.g. `| head`
            import os
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0

    if a.cmd == "client-build":
        from . import clientbuild
        return clientbuild.cli(a.dest, a.skip, a.force)

    if a.cmd == "client-mode":
        from . import settings
        try:
            _print(settings.set_client_mode(a.value) if a.value else settings.client_mode())
        except settings.SettingsError as e:
            log(str(e))
            return 1
        return 0

    if a.cmd == "dock-policy":
        from . import settings
        try:
            _print(settings.set_dock_policy(a.value) if a.value else settings.dock_policy())
        except settings.SettingsError as e:
            log(str(e))
            return 1
        return 0

    if a.cmd == "app":
        from . import app
        return app.cli()

    if a.cmd == "bootstrap":
        from . import bootstrap
        return bootstrap.main(a.client, a.yes, a.check, a.allow_test_inject, a.jobs, a.ignore_power)

    if a.cmd == "download":
        from . import downloads
        return downloads.cli(a.name)

    if a.cmd == "vanilla-mode":
        from . import settings
        try:
            _print(settings.vanilla_mode())
        except settings.SettingsError as e:
            log(str(e))
            return 1
        return 0

    if a.cmd == "timing-baseline":
        from . import timingbase
        return timingbase.cli(a)
    if a.cmd == "lane":
        from . import lanestack
        return lanestack.cli(a.action, force=a.force)
    if a.cmd == "icon":
        from . import icon
        try:
            _print(icon.generate(out_dir=a.out_dir))
        except icon.IconError as e:
            log(f"ICON FAILED: {e}")
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
