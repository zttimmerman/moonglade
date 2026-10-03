"""M2 population session: one warm-up shared by the pop.* and perf.* checks (m2-living-world tasks 2.1, 4.1).

Timeline (all checks read the same session, cached in the runner's ctx):
  T0           stack down, then `hs up --profile population` (cold worldserver; the warm-up is measured from here)
  T0..T0+W     warm-up (W = WARMUP_S <= 10 min, the spec's ceiling); online random-bot count polled for evidence
  T0+W         snapshot S0: `.saveall` + barrier, then every online random bot's zone/position/level/xp/quests
  T0+W..+10m   perf sampler thread: every 10 s worldserver/mysqld RSS, host free+inactive, `.server info`
  S0+5 min     snapshot S1 (same fields) -> size and activity deltas
The whole session runs under keep_awake (caffeinate), F12.

Data provenance (CLAUDE.md §7): zone IDs come from the client's AreaTable.dbc and the world DB's playercreateinfo
at runtime (see targets()); nothing here hard-codes a zone, map or area ID.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path

from . import botlog, config, db, dbc, paths as P, soap, stack
from .util import awake_assertions, keep_awake, log

HS = str(P.HARNESS / "hs")
WARMUP_S = 570            # spec: "after a warm-up of at most 10 minutes from `hs up`"; 30 s left for S0's saveall
WARMUP_MAX_S = 600        # S0's `.saveall` (the moment positions are captured) must start within this
SAMPLE_EVERY_S = 10       # spec: perf samples every 10 s ...
SAMPLE_WINDOW_S = 600     # ... for 10 minutes after warm-up
ACTIVITY_GAP_S = 300      # spec: count/sample again 5 minutes later
SAVE_BARRIER_TIMEOUT_S = 120

# Budget limits (performance-budget delta as revised 2026-10-03, "Server-side budget under the population profile").
# Process memory = max(RSS, phys_footprint) (mem_gb). Host free+inactive is recorded, not limited.
LIMITS = {"worldserver_mem_gb": ("<=", 6.0), "mysqld_mem_gb": ("<=", 1.0), "stack_total_gb": ("<=", 7.0),
          "update_p95_ms": ("<=", 100), "update_median_ms": ("<=", 50)}
RECORDED = ("host_free_inactive_gb",)


class PopulationError(RuntimeError):
    def __init__(self, msg, evidence=None):
        super().__init__(msg)
        self.evidence = evidence or {}


# ----------------------------------------------------------------------------------------------- zone targets

def _faction_capitals(areas: dict) -> list[dict]:
    """Rule: AreaTable rows that are zones (parent 0), carry AREA_FLAG_CAPITAL (DBCEnums.h:242), and belong to one
    faction (team ALLY or HORDE, DBCEnums.h:56-59). Neutral capital-flagged cities (team ANY) are not a faction's
    capital city."""
    return sorted(({"id": a["id"], "name": a["name"], "map": a["map"], "kind": "capital",
                    "team": {dbc.AREATEAM_ALLY: "Alliance", dbc.AREATEAM_HORDE: "Horde"}[a["team"]],
                    "source": f"AreaTable.dbc ID {a['id']}: parent 0, flags 0x{a['flags']:x} has AREA_FLAG_CAPITAL "
                              f"0x{dbc.AREA_FLAG_CAPITAL:x}, team {a['team']}"}
                   for a in areas.values()
                   if a["parent"] == 0 and a["flags"] & dbc.AREA_FLAG_CAPITAL
                   and a["team"] in (dbc.AREATEAM_ALLY, dbc.AREATEAM_HORDE)), key=lambda z: z["id"])


def _starting_zones(areas: dict) -> list[dict]:
    """Rule: for each playable race in world.playercreateinfo, the start zone shared by most of its classes (the
    class-specific exception, a hero class's own start zone, is outvoted), mapped to its top-level zone through
    AreaTable.dbc parents. Races that share a starting zone count once."""
    rows = db.query("SELECT race, class, map, zone FROM playercreateinfo", "world")
    by_race: dict[int, Counter] = {}
    for race, _cls, _m, zone in rows:
        by_race.setdefault(int(race), Counter())[int(zone)] += 1
    zones: dict[int, dict] = {}
    for race, ctr in sorted(by_race.items()):
        (zone, n), = ctr.most_common(1)
        top = dbc.top_zone(areas, zone)
        if top is None:
            raise PopulationError(f"playercreateinfo zone {zone} (race {race}) not in AreaTable.dbc")
        z = zones.setdefault(top, {"id": top, "name": areas[top]["name"], "map": areas[top]["map"],
                                   "kind": "starting zone", "races": [], "source": ""})
        z["races"].append(race)
        z["source"] = (f"world.playercreateinfo zone of races {z['races']} (most classes) -> AreaTable.dbc top zone "
                       f"{top}" + (f" (from area {zone})" if zone != top else ""))
    return sorted(zones.values(), key=lambda z: z["id"])


def targets() -> dict:
    """The 8 race starting zones and 8 capital cities, derived from data at runtime (rules in the docstrings)."""
    areas = dbc.area_table()
    return {"starting_zones": _starting_zones(areas), "capitals": _faction_capitals(areas),
            "rules": {"starting_zones": _starting_zones.__doc__.split("Rule: ", 1)[1].strip(),
                      "capitals": _faction_capitals.__doc__.split("Rule: ", 1)[1].strip()},
            "dbc": str((dbc.DBC_DIR / "AreaTable.dbc").relative_to(P.ROOT))}


# ----------------------------------------------------------------------------------------------- bot state

RNDBOT_ACCOUNTS = (f"SELECT account_id FROM {P.DB_NAMES['playerbots']}.playerbots_account_type "
                   "WHERE account_type = 1")       # 1 = RNDbot (column comment in playerbots_account_type)


FRESH_LEVEL = 1     # < 3, so Randomize() takes the RandomizeFirst (re-roll) path (RandomPlayerbotMgr.cpp:1950)


def fresh_start() -> dict:
    """"Fresh world" step of the population profile, run by stack.up before the worldserver starts.

    Why: mod-playerbots persists each bot's level and its next randomize/teleport time (playerbots_random_bots,
    1-5 h teleport interval, 2 h-14 d randomize interval; RandomPlayerbotMgr.cpp:1521-1571). Without this, a boot
    resumes every bot where the last session left it: bots drift out of capitals and level out of starting zones over
    accumulated play, so what a player (and the checks) see depends on history. With it, every boot re-rolls the
    random bots like a new realm: level 1 makes Randomize() take the RandomizeFirst path (RandomPlayerbotMgr.cpp:
    1945-1951: random level per the profile's level knobs, then RandomTeleportForLevel), and dropping the
    randomize/teleport timers makes that happen seconds after login (ProcessBot, :1389-1399).

    Touches only random-bot characters (accounts with playerbots_account_type = 1) and their playerbots_random_bots
    timer rows; never another account's data (non_bot_digest is compared before/after by pop.size_stable)."""
    guids = f"SELECT guid FROM characters WHERE account IN ({RNDBOT_ACCOUNTS})"
    before = non_bot_digest()
    n = int(db.scalar(f"SELECT COUNT(*) FROM characters WHERE account IN ({RNDBOT_ACCOUNTS})", "characters") or 0)
    db.query(f"UPDATE characters SET level = {FRESH_LEVEL}, xp = 0 WHERE account IN ({RNDBOT_ACCOUNTS}) AND online = 0",
             "characters")
    ids = [r[0] for r in db.query(guids, "characters")]
    deleted = 0
    if ids:
        deleted = int(db.scalar(f"SELECT COUNT(*) FROM playerbots_random_bots WHERE owner = 0 AND event IN "
                                f"('randomize', 'teleport') AND bot IN ({','.join(ids)})", "playerbots") or 0)
        db.query(f"DELETE FROM playerbots_random_bots WHERE owner = 0 AND event IN ('randomize', 'teleport') "
                 f"AND bot IN ({','.join(ids)})", "playerbots")
    levels = db.query(f"SELECT level, COUNT(*) FROM characters WHERE account IN ({RNDBOT_ACCOUNTS}) GROUP BY level",
                      "characters")
    left = int(db.scalar(f"SELECT COUNT(*) FROM playerbots_random_bots WHERE owner = 0 AND event IN "
                         f"('randomize', 'teleport') AND bot IN ({','.join(ids)})", "playerbots") or 0) if ids else 0
    return {"random_bot_characters_reset": n, "timer_rows_deleted": deleted, "reset_level": FRESH_LEVEL,
            "bot_levels_after": {int(lv): int(c) for lv, c in levels}, "timer_rows_left": left,
            "non_bot_before": before, "non_bot_after": non_bot_digest()}


def online_bots() -> int:
    return int(db.scalar(f"SELECT COUNT(*) FROM characters WHERE online = 1 AND account IN ({RNDBOT_ACCOUNTS})",
                         "characters") or 0)


def _totaltimes() -> dict[int, int]:
    rows = db.query(f"SELECT guid, totaltime FROM characters WHERE online = 1 AND account IN ({RNDBOT_ACCOUNTS})",
                    "characters")
    return {int(g): int(t) for g, t in rows}


def save_all() -> dict:
    """SOAP `.saveall`, then wait until it reached the DB. Barrier: Player::SaveToDB writes `totaltime` (played
    seconds, ticking every second), so a bot whose row shows a larger totaltime than just before the save has been
    saved again. Waits for >= 98% of the bots online before and after (logins/logouts in between are excluded)."""
    before = _totaltimes()
    time.sleep(1.1)                    # make sure every played-time counter has moved past `before`
    t0 = time.time()
    r = soap.gm("saveall", timeout=60)
    want, advanced, after = 0, 0, {}
    while time.time() - t0 < SAVE_BARRIER_TIMEOUT_S:
        after = _totaltimes()
        common = [g for g in before if g in after]
        want = len(common)
        advanced = sum(1 for g in common if after[g] > before[g])
        if want and advanced >= 0.98 * want:
            break
        time.sleep(2)
    ev = {"gm": r.evidence(), "gm_at": round(t0, 1), "bots_compared": want, "bots_saved": advanced, "barrier_s": round(time.time() - t0, 1)}
    if not want or advanced < 0.98 * want:
        raise PopulationError("saveall barrier: bots' totaltime did not advance", ev)
    return ev


def snapshot() -> dict:
    """`.saveall` + barrier, then every online random bot: name, race, level, xp, map, zone, position, quests
    rewarded. Times are wall-clock epoch seconds."""
    save = save_all()
    rows = db.query(
        "SELECT c.guid, c.name, c.race, c.level, c.xp, c.map, c.zone, c.position_x, c.position_y, c.position_z, "
        "(SELECT COUNT(*) FROM character_queststatus_rewarded q WHERE q.guid = c.guid) "
        f"FROM characters c WHERE c.online = 1 AND c.account IN ({RNDBOT_ACCOUNTS})", "characters")
    bots = {int(g): {"name": n, "race": int(ra), "level": int(lv), "xp": int(xp), "map": int(m), "zone": int(z),
                     "x": float(x), "y": float(y), "z": float(zz), "rewarded": int(q)}
            for g, n, ra, lv, xp, m, z, x, y, zz, q in rows}
    return {"t": time.time(), "save": save, "bots": bots}


# ----------------------------------------------------------------------------------------------- perf samples

def _rss_gb(pid: int | None) -> float | None:
    if not pid:
        return None
    r = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    return round(int(r.stdout.strip()) / 2**20, 3) if r.stdout.strip() else None   # ps rss is KiB


_libproc = None


def footprint_gb(pid: int | None) -> dict | None:
    """proc_pid_rusage(RUSAGE_INFO_V0): ri_resident_size and ri_phys_footprint (<sys/resource.h> struct
    rusage_info_v0: uuid[16], then uint64 user_time, system_time, pkg_idle_wkups, interrupt_wkups, pageins,
    wired_size, resident_size, phys_footprint, ...). phys_footprint is what Activity Monitor/top call "Memory":
    it includes pages macOS has compressed or swapped, which RSS doesn't."""
    global _libproc
    import ctypes
    import struct
    if not pid:
        return None
    _libproc = _libproc or ctypes.CDLL("/usr/lib/libproc.dylib")
    buf = ctypes.create_string_buffer(512)
    if _libproc.proc_pid_rusage(int(pid), 0, buf) != 0:
        return None
    v = struct.unpack_from("<16s12Q", buf.raw)
    return {"resident_gb": round(v[7] / 2**30, 3), "footprint_gb": round(v[8] / 2**30, 3)}


def mem_gb(rss_gb: float | None, fp: dict | None) -> float | None:
    """The budget's process memory: the larger of RSS and phys_footprint. RSS alone undercounts when macOS
    compresses or swaps the process (measured 2026-10-02 with 10 bots: worldserver RSS 0.885 GB vs footprint 3.37 GB
    while 8.5 GB of swap was in use), so the conservative reading counts compressed pages too."""
    vals = [v for v in (rss_gb, (fp or {}).get("footprint_gb")) if v is not None]
    return max(vals) if vals else None


def host_memory() -> dict:
    """vm_stat: free + inactive pages (the budget's "host free plus inactive memory"), plus speculative for context."""
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+) bytes", out).group(1))
    pages = {k: int(v) for k, v in re.findall(r"^Pages (\w[\w ]*?):\s+(\d+)\.", out, re.M)}
    gb = lambda n: round(n * page / 2**30, 3)   # noqa: E731
    return {"free_gb": gb(pages.get("free", 0)), "inactive_gb": gb(pages.get("inactive", 0)),
            "speculative_gb": gb(pages.get("speculative", 0)),
            "free_inactive_gb": gb(pages.get("free", 0) + pages.get("inactive", 0))}


SERVER_INFO_RE = {
    # cs_server.cpp HandleServerInfoCommand (vendor/azerothcore-wotlk/src/server/scripts/Commands/cs_server.cpp:276-289)
    "players": re.compile(r"Characters in world: (\d+)"),
    "last_ms": re.compile(r"Update time diff: (\d+)ms\. Last (\d+) diffs summary"),
    "mean_ms": re.compile(r"\|- Mean: (\d+)ms"),
    "median_ms": re.compile(r"\|- Median: (\d+)ms"),
    "pcts_ms": re.compile(r"\|- Percentiles \(95, 99, max\): (\d+)ms, (\d+)ms, (\d+)ms"),
}


def parse_server_info(text: str) -> dict:
    """World update-time statistics the server itself reports: the last diff and mean/median/p95/p99/max over its
    last AVG_DIFF_COUNT (500, UpdateTime.h:26) world-loop diffs."""
    out = {}
    m = SERVER_INFO_RE["players"].search(text)
    out["characters_in_world"] = int(m.group(1)) if m else None
    m = SERVER_INFO_RE["last_ms"].search(text)
    out["last_ms"], out["diffs"] = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    for k in ("mean_ms", "median_ms"):
        m = SERVER_INFO_RE[k].search(text)
        out[k] = int(m.group(1)) if m else None
    m = SERVER_INFO_RE["pcts_ms"].search(text)
    out["p95_ms"], out["p99_ms"], out["max_ms"] = (int(m.group(i)) for i in (1, 2, 3)) if m else (None,) * 3
    return out


def perf_sample() -> dict:
    st = stack.stack_processes()
    ws, my = (st["worldserver"] or [None])[0], (st["mysqld"] or [None])[0]
    try:
        info = soap.gm("server info", timeout=30)
        si = parse_server_info(info.output) if info.ok else {"error": info.output[-300:]}
    except OSError as e:
        si = {"error": f"SOAP unreachable: {e}"}
    au = (st["authserver"] or [None])[0]
    wr, mr, wf, mf = _rss_gb(ws), _rss_gb(my), footprint_gb(ws), footprint_gb(my)
    ar, af = _rss_gb(au), footprint_gb(au)
    return {"t": round(time.time(), 1), "worldserver_pid": ws,
            "worldserver_gb": mem_gb(wr, wf), "worldserver_ps_rss_gb": wr, "worldserver_rusage": wf,
            "mysqld_gb": mem_gb(mr, mf), "mysqld_ps_rss_gb": mr, "mysqld_rusage": mf,
            "authserver_gb": mem_gb(ar, af), "authserver_ps_rss_gb": ar, "authserver_rusage": af,
            "host": host_memory(), "swap_used_gb": swap_used_gb(), "server_info": si}


def swap_used_gb() -> float | None:
    out = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True).stdout
    m = re.search(r"used = ([\d.]+)M", out)
    return round(float(m.group(1)) / 1024, 2) if m else None


def sample_values(s: dict) -> dict:
    """One perf sample reduced to the budget's quantities (None when unreadable: that fails the budget)."""
    si = s.get("server_info", {})
    parts = [s.get("worldserver_gb"), s.get("mysqld_gb"), s.get("authserver_gb")]
    return {"worldserver_mem_gb": s.get("worldserver_gb"), "mysqld_mem_gb": s.get("mysqld_gb"),
            "stack_total_gb": round(sum(parts), 3) if None not in parts else None,
            "update_p95_ms": si.get("p95_ms"), "update_median_ms": si.get("median_ms"),
            "host_free_inactive_gb": s.get("host", {}).get("free_inactive_gb")}


def violations(samples: list[dict], limits: dict = LIMITS) -> list[str]:
    """Every sample must meet every limit ("every limit above holds" over the 10-minute window)."""
    bad = []
    for i, s in enumerate(samples):
        vals = sample_values(s)
        for k, (op, lim) in limits.items():
            v = vals.get(k)
            if v is None or (op == "<=" and v > lim) or (op == ">=" and v < lim):
                bad.append(f"sample {i} (t+{s['t'] - samples[0]['t']:.0f}s): {k}={v} (limit {op} {lim})")
    return bad


def summarize(samples: list[dict]) -> dict:
    """Per quantity: min / median / max over the window (the tuning record's columns)."""
    out = {}
    for k in (*LIMITS, *RECORDED):
        vals = [v for v in (sample_values(s)[k] for s in samples) if v is not None]
        out[k] = ({"min": min(vals), "median": round(statistics.median(vals), 3), "max": max(vals), "n": len(vals)}
                  if vals else {"n": 0})
    return out


class _Sampler(threading.Thread):
    def __init__(self, duration_s=SAMPLE_WINDOW_S, every_s=SAMPLE_EVERY_S):
        super().__init__(daemon=True)
        self.duration_s, self.every_s, self.samples, self.errors = duration_s, every_s, [], []

    def run(self):
        t0 = time.time()
        k = 0
        while k * self.every_s <= self.duration_s:
            delay = t0 + k * self.every_s - time.time()
            if delay > 0:
                time.sleep(delay)
            try:
                self.samples.append(perf_sample())
            except Exception as e:  # noqa: BLE001  (recorded; a missing sample fails the budget)
                self.errors.append(f"{type(e).__name__}: {e}")
            k += 1


# ----------------------------------------------------------------------------------------------- non-bot data

def non_bot_digest() -> dict:
    """Checksums of every non-bot account and its characters (the population must never touch them)."""
    acc = db.query(f"SELECT COUNT(*), COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', id, username, salt, verifier, email, "
                   f"expansion))), 0) FROM account WHERE id NOT IN (SELECT account_id FROM "
                   f"{P.DB_NAMES['playerbots']}.playerbots_account_type)", "auth")[0]
    ch = db.query("SELECT COUNT(*), COALESCE(BIT_XOR(CRC32(CONCAT_WS('|', guid, account, name, race, class, level, "
                  "xp, money, map, zone, position_x, position_y, position_z, totaltime))), 0) FROM characters WHERE "
                  f"account NOT IN (SELECT account_id FROM {P.DB_NAMES['playerbots']}.playerbots_account_type)",
                  "characters")[0]
    return {"accounts": {"count": int(acc[0]), "crc": acc[1]}, "characters": {"count": int(ch[0]), "crc": ch[1]}}


def db_sizes_mb() -> dict:
    rows = db.query("SELECT table_schema, ROUND(SUM(data_length + index_length) / 1048576, 1) FROM "
                    "information_schema.tables WHERE table_schema LIKE 'acore_%' GROUP BY table_schema")
    du = subprocess.run(["du", "-sk", str(P.MYSQL_DATA)], capture_output=True, text=True).stdout.split()
    return {"schemas_mb": {s: float(v) for s, v in rows}, "datadir_mb": round(int(du[0]) / 1024, 1) if du else None,
            "disk_free_gb": round(shutil.disk_usage(P.ROOT).free / 2**30, 2)}


# ----------------------------------------------------------------------------------------------- the session

FATAL_RE = r"\bFATAL\b|ASSERTION FAILED"


def run_session(warmup_s: int = WARMUP_S, window_s: int = SAMPLE_WINDOW_S, gap_s: int = ACTIVITY_GAP_S) -> dict:
    """Cold start with the population profile, warm up, snapshot, sample. Returns the session evidence."""
    with keep_awake() as awake:
        ev: dict = {"profile": "population", "n": config.population_n(),
                    "profile_overrides": config.overrides("population"), "keep_awake": awake}
        ev["down_before"] = stack.down() if any(stack.stack_processes().values()) else "already down"
        # non-bot data and DB size before the population exists in this run (mysqld only)
        stack.mysql_up()
        ev["non_bot_before"] = non_bot_digest()
        ev["targets"] = targets()     # the zone set every check of this session evaluates against
        ev["db_before"] = db_sizes_mb()
        t0 = time.time()
        r = subprocess.run([HS, "up", "--profile", "population"], capture_output=True, text=True, timeout=3600)
        ev["up"] = {"cmd": "harness/hs up --profile population", "exit": r.returncode,
                    "elapsed_s": round(time.time() - t0, 1), "stderr_tail": r.stderr[-1500:]}
        try:
            info = json.loads(r.stdout)
            ev["up"]["fresh_start"] = info.get("fresh_start")
            ev["up"]["profile"] = info.get("profile")
        except ValueError:
            ev["up"]["stdout_tail"] = r.stdout[-1500:]
        if r.returncode != 0:
            raise PopulationError("hs up --profile population failed", ev)
        rp = stack.running_profile()
        ev["running_profile"] = rp
        if not rp or rp["profile"] != "population":
            raise PopulationError("worldserver is not running the population profile", ev)
        ws_pid = rp["pid"]
        ev["awake_assertions"] = awake_assertions(awake.get("pid"))
        ramp = []
        while time.time() - t0 < warmup_s:
            ramp.append({"t_s": round(time.time() - t0), "online_random_bots": online_bots()})
            time.sleep(min(30, max(0, warmup_s - (time.time() - t0))))
        ev["warmup"] = {"warmup_s": warmup_s, "ramp": ramp}

        sampler = _Sampler(window_s)
        sampler.start()
        log_mark = 0                    # Playerbots.log is truncated at worldserver start: whole file = this run
        s0 = snapshot()
        s0["t_rel_s"] = round(s0["t"] - t0, 1)
        log(f"population: S0 at t+{s0['t_rel_s']}s, {len(s0['bots'])} online random bots")
        time.sleep(max(0, s0["t"] + gap_s - time.time()))
        s1 = snapshot()
        s1["t_rel_s"] = round(s1["t"] - t0, 1)
        log(f"population: S1 at t+{s1['t_rel_s']}s, {len(s1['bots'])} online random bots")
        sampler.join(timeout=window_s + 120)
        st = stack.stack_processes()
        ev["worldserver"] = {"pid_at_up": ws_pid, "pids_at_end": st["worldserver"],
                             "same_process": st["worldserver"] == [ws_pid]}
        ev["logs"] = {
            "playerbots_fatal": botlog.grep(FATAL_RE, log_mark)[:20],
            "playerbots_error_count": botlog.count(r"\bERROR\b", log_mark),
            "playerbots_error_sample": botlog.grep(r"\bERROR\b", log_mark)[:10],
            "server_fatal": botlog.grep(FATAL_RE, 0, P.LOGS / "Server.log")[:20],
            "worldserver_err_fatal": botlog.grep(FATAL_RE, 0, P.LOGS / "worldserver.err")[:20],
            "cant_login_all": botlog.grep(r"Can't log-in all the requested bots", log_mark)[:5],
            "playerbots_log_mb": round(botlog.offset() / 2**20, 1),
        }
        ev["samples"] = sampler.samples
        ev["sampler_errors"] = sampler.errors
        ev["s0"], ev["s1"] = s0, s1
        ev["non_bot_after"] = non_bot_digest()
        ev["db_after"] = db_sizes_mb()
        ev["t0"] = t0
        return ev


def save_session(s: dict) -> Path:
    """Keep the full session (all samples and both snapshots) as harness/out/pop-session-N<N>-<epoch>.json, so a
    run's numbers can be replayed or copied into the tuning record."""
    P.OUT.mkdir(parents=True, exist_ok=True)
    out = P.OUT / f"pop-session-N{s['n']}-{int(s['t0'])}.json"
    out.write_text(json.dumps(s, indent=1, default=str))
    s["session_file"] = str(out.relative_to(P.ROOT))
    return out


def session(ctx: dict) -> dict:
    """The shared session for this run (computed once; a failure is re-raised to every check that needs it)."""
    if "population_session" not in ctx:
        try:
            ctx["population_session"] = run_session(
                warmup_s=int(os.environ.get("HS_POP_WARMUP_S", WARMUP_S)),
                window_s=int(os.environ.get("HS_POP_WINDOW_S", SAMPLE_WINDOW_S)),
                gap_s=int(os.environ.get("HS_POP_GAP_S", ACTIVITY_GAP_S)))
            save_session(ctx["population_session"])
        except Exception as e:  # noqa: BLE001
            ctx["population_session"] = e
    s = ctx["population_session"]
    if isinstance(s, Exception):
        raise s
    return s


# ----------------------------------------------------------------------------------------------- CLI (tuning runs)

def brief(s: dict) -> dict:
    """Session reduced to what the tuning record and a human need (full session JSON is kept separately)."""
    from .checks.population_checks import activity, zone_coverage
    rows, probs = zone_coverage(s["s0"], s.get("targets") or targets())
    return {"n": s["n"], "up_s": s["up"]["elapsed_s"], "online_s0": len(s["s0"]["bots"]),
            "online_s1": len(s["s1"]["bots"]), "zone_problems": probs,
            "zones": {f"{r['zone_id']} {r['name']}": r["bots"] for r in rows},
            "activity": {k: v for k, v in activity(s["s0"], s["s1"]).items() if k != "samples"},
            "summary": summarize(s["samples"]), "violations": violations(s["samples"])[:20],
            "logs": {k: v for k, v in s["logs"].items() if k != "playerbots_error_sample"},
            "db_before": s["db_before"], "db_after": s["db_after"],
            "non_bot_unchanged": s["non_bot_before"] == s["non_bot_after"]}


def cli(n: int | None, leave_up: bool = False) -> dict:
    """`hs pop-session [--n N]`: one population session (cold start, warm-up, sampling) at N bots; writes the full
    session to harness/out/pop-session-N<N>-<epoch>.json and returns the brief. Stops the stack unless leave_up."""
    if n:
        os.environ[config.POPULATION_N_ENV] = str(n)
    try:
        s = run_session()
        save_session(s)
        return brief(s) | {"session_file": s["session_file"]}
    finally:
        if not leave_up:
            stack.down()
