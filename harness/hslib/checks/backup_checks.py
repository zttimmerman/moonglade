"""save-backups: backups on quit, rotation, failures, kill-safety, restore, and the player's backups left alone
(openspec/changes/save-backups).

Every check works in a scratch backup folder under env/tmp/ (named through $HS_BACKUP_DIR plus the explicit test flag
--allow-test-client-dir, or passed to backups.* directly) and never reads or writes env/backups/. Sessions are the
play.* checks' headless ./play with the .bat stand-in and the harness test account (HSTEST). Where a check needs the
player's WTF folder it uses a scratch play client (env/check-play-backups/, $HS_PLAY_CLIENT_DIR), never
env/client-play/.

Restores never touch the live databases (spec-change log 2026-10-06): backups are taken of the live databases (read
only), and every restore a check makes targets scratch schemas named hsbk_<check>_<db> (`--target hsbk_<check>`),
which the check seeds from a backup, changes, and drops at the end. The checks that restore compare every live
table's CHECKSUM TABLE before and after. A failed check keeps its scratch backup folder under env/tmp/ for
inspection.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .. import backups as B, clientbuild, launch, paths as P, play, stack, stages as S
from ..registry import check, require
from .play_checks import PINNED, PLAY, StandIn, human_lines, is_clear, last_record, remaining

CAP = "save-backups"
SCRATCH_CLIENT = P.ENV / "check-play-backups"      # scratch play client (WTF), never env/client-play/
STACK_SAMPLE_S = 0.1
NEW_CHAR = "Hsbkpnew"                                # restore_roundtrip's extra test character (letters only)


# ---------------------------------------------------------------- helpers

def scratch_root(tag: str) -> Path:
    d = P.ENV / "tmp" / f"hs-backups-{tag}-{os.getpid()}"
    shutil.rmtree(d, ignore_errors=True)
    d.parent.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def drop(*paths: Path) -> None:
    for p in paths:
        if p and (Path(p).resolve().is_relative_to((P.ENV / "tmp").resolve()) or Path(p).resolve() == SCRATCH_CLIENT.resolve()):
            shutil.rmtree(p, ignore_errors=True)


def scratch_client(seed: str) -> dict:
    """Build the scratch play client and give HSTEST a WTF account folder with run-unique files."""
    shutil.rmtree(SCRATCH_CLIENT, ignore_errors=True)
    b = clientbuild.build(dest=SCRATCH_CLIENT)
    acct = SCRATCH_CLIENT / "WTF" / "Account" / P.TEST_USER
    shutil.rmtree(acct, ignore_errors=True)
    (acct / "SavedVariables").mkdir(parents=True)
    (acct / "bindings-cache.wtf").write_text(f"bindingmode 0\nbind W MOVEFORWARD\n-- {seed}\n")
    (acct / "macros-cache.txt").write_text(f"MACRO 1 \"hs\" Ability_Ambush\n/say {seed}\nEND\n")
    (acct / "SavedVariables" / "Blizzard_CombatLog.lua").write_text(f"-- {seed}\nBlizzard_CombatLog_Filters = {{}}\n")
    return {"dir": str(SCRATCH_CLIENT), "built": b["built"], "wtf": str(acct), "files": B.wtf_hashes(acct)}


def wtf_dir() -> Path:
    return SCRATCH_CLIENT / "WTF" / "Account" / P.TEST_USER


def manifest_wtf(m: dict) -> dict:
    return {k[len("wtf/"):]: v["sha256"] for k, v in m["files"].items() if k.startswith("wtf/")}


def live_stats() -> dict:
    """CHECKSUM TABLE + row count for every table of the three databases (mysqld must be up)."""
    return {k: B.table_stats(P.DB_NAMES[k]) for k in B.DATABASES}


def diff_stats(a: dict, b: dict) -> list:
    out = []
    for k in B.DATABASES:
        for t in sorted(set(a.get(k, {})) | set(b.get(k, {}))):
            if a.get(k, {}).get(t) != b.get(k, {}).get(t):
                out.append({"db": k, "table": t, "a": a.get(k, {}).get(t), "b": b.get(k, {}).get(t)})
    return out


def manifest_stats(m: dict) -> dict:
    return {k: m["databases"][k]["tables"] for k in B.DATABASES}


def ids(root: Path) -> list[str]:
    return [m["id"] for m in B.list_backups(root)]


def leftovers_in(root: Path) -> list[str]:
    return sorted(p.name for p in Path(root).iterdir() if p.name.startswith((".tmp-", ".del-"))) if Path(root).is_dir() else []


def clone_backup(src: dict, root: Path, when: datetime, kind: str) -> dict:
    """A copy of a real backup (APFS clone of its files) under another id/kind: same data, valid checksums."""
    bid = when.strftime("%Y%m%d-%H%M%S") + f"-{kind}"
    dst = Path(root) / bid
    r = subprocess.run(["cp", "-cR", src["path"], str(dst)], capture_output=True, text=True)
    require(r.returncode == 0, f"couldn't clone a backup: {r.stderr.strip()}")
    m = json.loads((dst / "manifest.json").read_text())
    m |= {"id": bid, "kind": kind, "created": when.astimezone().isoformat(timespec="milliseconds"),
          "cloned_from": src["id"]}
    (dst / "manifest.json").write_text(json.dumps(m, indent=2))
    return m | {"path": str(dst)}


def given_clear(ev: dict) -> None:
    before = remaining()
    ev["given_nothing_running"] = before
    require(is_clear(before), "GIVEN not met: something from this repo is already running", **ev)


def shutdown_events(output: str) -> list[dict]:
    return [e for e in (S.parse_shutdown(l) for l in output.splitlines()) if e]


def backup_stage_order(evs: list[dict]) -> dict:
    """Where shutdown_backup sits among the [shutdown] events: after every shutdown_login event, before every
    shutdown_database event."""
    seq = [(e["id"], e["state"]) for e in evs]
    pos = {sid: [i for i, (x, _s) in enumerate(seq) if x == sid] for sid in S.SHUTDOWN_IDS}
    b = pos["shutdown_backup"]
    ok = bool(b) and bool(pos["shutdown_login"]) and bool(pos["shutdown_database"]) \
        and max(pos["shutdown_login"]) < min(b) and max(b) < min(pos["shutdown_database"])
    return {"sequence": seq, "backup_states": [seq[i][1] for i in b], "in_order": ok}


class StackSampler(threading.Thread):
    """stack_processes() every 0.1 s: when the worldserver was last seen and when mysqld was first seen gone."""

    def __init__(self):
        super().__init__(daemon=True, name="backup-stack-sampler")
        self.samples: list = []
        self.stop_ev = threading.Event()

    def run(self):
        while not self.stop_ev.is_set():
            t = time.time()
            try:
                v = {k: bool(x) for k, x in stack.stack_processes().items()}
            except Exception:  # noqa: BLE001
                v = None
            self.samples.append((round(t, 3), v))
            self.stop_ev.wait(STACK_SAMPLE_S)

    def stop(self):
        self.stop_ev.set()
        self.join(timeout=5)

    def summary(self) -> dict:
        ok = [(t, v) for t, v in self.samples if v is not None]
        ws = [t for t, v in ok if v["worldserver"]]
        my = [t for t, v in ok if v["mysqld"]]
        gone = [t for t, v in ok if not v["mysqld"] and my and t > max(my)]
        gaps = [b[0] - a[0] for a, b in zip(ok, ok[1:])]
        return {"samples": len(ok), "errors": len(self.samples) - len(ok),
                "worldserver_last_seen": max(ws) if ws else None, "mysqld_last_seen": max(my) if my else None,
                "mysqld_first_seen_gone": min(gone) if gone else None, "max_gap_s": round(max(gaps), 3) if gaps else None}


class Session:
    """One headless ./play session (stand-in, test account) with a scratch backup folder."""

    def __init__(self, tag: str, root: Path | None, settings: str = "stock", client: Path | None = None,
                 knobs: dict | None = None, extra: tuple = ("--allow-test-client-dir",)):
        self.s = StandIn(tag)
        self.sfile = self.s.dir / "settings.json"
        self.sfile.write_text(json.dumps({"client": settings}))
        self.env = {"HS_SETTINGS": str(self.sfile), **(knobs or {})}
        if root is not None:
            self.env["HS_BACKUP_DIR"] = str(root)
        if client is not None:
            self.env["HS_PLAY_CLIENT_DIR"] = str(client)
        self.extra = extra
        self.t_start = 0.0
        self.sampler: StackSampler | None = None

    def start(self) -> dict:
        self.t_start = time.time()
        self.s.start_play(*self.extra, env=self.env)
        up = self.s.wait_client_up()
        return up

    def quit_and_wait(self, sample: bool = False, timeout: int = 400) -> dict:
        if sample:
            self.sampler = StackSampler()
            self.sampler.start()
        self.s.stop_file.write_text("quit")
        rc, dt = self.s.wait_exit(timeout=timeout)
        if self.sampler:
            time.sleep(0.3)
            self.sampler.stop()
        return self.result(rc, dt)

    def result(self, rc, dt=None) -> dict:
        out = self.s.output()
        rec = last_record(self.s.proc.pid)
        return {"exit_code": rc, "exit_after_quit_s": dt, "output_tail": human_lines(out)[-2500:],
                "shutdown_events": shutdown_events(out),
                "notices": [l for l in out.splitlines() if l.startswith("[notice] ")],
                "record_backup": rec.get("backup"), "record_backup_plan": rec.get("backup_plan"),
                "record_outcome": rec.get("outcome"), "record_lock": rec.get("lock"),
                "stack_sampler": self.sampler.summary() if self.sampler else None,
                "remaining_after": remaining(self.s)}

    def cleanup(self):
        if self.sampler:
            self.sampler.stop()
        self.s.cleanup()


def backups_cli(*args: str, root: Path, client: Path | None = None, settings: str = "stock",
                flag: bool = True, timeout: int = 900) -> dict:
    """`./play --backups ...` as the app runs it (JSON), with the scratch folder."""
    sfile = root.parent / f"{root.name}-settings.json"
    sfile.write_text(json.dumps({"client": settings}))
    env = {**os.environ, **PINNED, "HS_SETTINGS": str(sfile), "HS_BACKUP_DIR": str(root)}
    env.pop("HS_PLAY_CLIENT_DIR", None)
    if client is not None:
        env["HS_PLAY_CLIENT_DIR"] = str(client)
    cmd = [PLAY, "--backups", *args, "--json", *(["--allow-test-client-dir"] if flag else [])]
    t0 = time.time()
    r = subprocess.run(cmd, cwd=P.ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    sfile.unlink(missing_ok=True)
    try:
        js = json.loads(r.stdout)
    except ValueError:
        js = None
    return {"cmd": " ".join(cmd[1:]), "rc": r.returncode, "json": js, "stdout_tail": r.stdout[-1500:],
            "stderr_tail": r.stderr[-1500:], "t_start": round(t0, 3), "t_end": round(time.time(), 3)}


def stack_clear() -> dict:
    return {k: v for k, v in stack.stack_processes().items() if v}


# ---------------------------------------------------------------- backups.on_quit

@check("backups.on_quit", order=365, capability=CAP, stack="down", tier="full", backups=True)
def on_quit(ctx):
    """Empty scratch backup folder + scratch play client: a ./play session where the test character gains a level
    ends with exactly one new backup whose manifest lists the three databases and the WTF folder with sha256 values
    matching the files (and the client's WTF files), lists the character at its new level, started after the
    worldserver was last seen and finished before mysqld was first seen gone (sampled every 0.1 s), with the
    shutdown_backup stage between login and database, and the stack fully down afterwards. Then each dump, loaded
    into scratch schemas, has every table's row count and CHECKSUM TABLE equal to the live databases (unchanged since
    the backup: the stack was down) and to the manifest."""
    from .. import fixtures as F
    ev: dict = {}
    given_clear(ev)
    root = scratch_root("onquit")
    seed = f"hs-onquit-{time.time():.3f}"
    sess = None
    loaded: list[str] = []
    try:
        ev["scratch_client"] = scratch_client(seed)
        require(not ids(root), "GIVEN not met: the scratch backup folder isn't empty", **ev)
        sess = Session("bkonquit", root, settings="patched", client=SCRATCH_CLIENT)
        up = sess.start()
        ev["client_up"] = up
        require(up["up"], f"the session's stand-in never came up: {up.get('why')}", output=sess.s.output()[-2000:], **ev)
        guid = F.char_guid()
        lv0 = F.char_state(guid)["level"]
        if lv0 >= 80:
            ev["reset_to_1"] = F.set_level(1)
            lv0 = 1
        ev["level_up"] = F.set_level(lv0 + 1)
        ev["level_before_after"] = [lv0, lv0 + 1]
        require(ev["level_up"]["saved"], "GIVEN not met: the character didn't gain a level", **ev)
        res = sess.quit_and_wait(sample=True)
        ev["session"] = res
        require(res["exit_code"] == 0, f"./play exited {res['exit_code']}", **ev)
        require(is_clear(res["remaining_after"]), "the stack (or something else) is still running after the session", **ev)
        listed = B.list_backups(root)
        ev["listed"] = [m["id"] for m in listed]
        require(len(listed) == 1, f"expected exactly one new backup, found {len(listed)}", **ev)
        m = listed[0]
        ev["manifest"] = {k: m.get(k) for k in ("id", "kind", "created", "started_at", "finished_at", "duration_s",
                                                "account", "characters", "worldserver_running_at_start", "bytes",
                                                "core_sha", "playerbots_sha", "wtf")}
        ev["manifest"]["databases"] = {k: {x: v.get(x) for x in ("schema", "file", "dump_s", "float_tables")}
                                       for k, v in m["databases"].items()}
        ev["manifest_files"] = m["files"]
        # files: every listed file hashes to its manifest value (recomputed here), the three dumps and WTF present
        actual = {rel: B.sha256_file(Path(m["path"]) / rel) for rel in m["files"]}
        bad = [rel for rel in m["files"] if actual[rel] != m["files"][rel]["sha256"]]
        on_disk = sorted(str(p.relative_to(m["path"])) for p in Path(m["path"]).rglob("*") if p.is_file())
        ev |= {"hash_mismatches": bad, "files_on_disk": on_disk}
        require(set(m["databases"]) == {"characters", "auth", "playerbots"}
                and {m["databases"][k]["schema"] for k in m["databases"]} ==
                {"acore_characters", "acore_auth", "acore_playerbots"}, "the manifest doesn't list the three databases", **ev)
        require(all(m["databases"][k]["file"] in m["files"] for k in B.DATABASES), "a dump file isn't in the manifest", **ev)
        require(not bad, "a file doesn't match its manifest sha256", **ev)
        require(sorted(set(on_disk) - {"manifest.json"}) == sorted(m["files"]), "files on disk aren't the manifest's", **ev)
        client_wtf = B.wtf_hashes(wtf_dir())
        ev["wtf_compare"] = {"client": client_wtf, "backup": manifest_wtf(m)}
        require(client_wtf and manifest_wtf(m) == client_wtf and (m.get("wtf") or {}).get("present"),
                "the backup's WTF folder doesn't match the play client's WTF/Account/HSTEST", **ev)
        mine = [c for c in m["characters"] if c["name"] == P.TEST_CHAR]
        require(mine and mine[0]["level"] == lv0 + 1, "the manifest doesn't list the character at its new level", **ev)
        # timing: after the worldserver exited, before mysqld stopped (independent 0.1 s samples)
        smp = res["stack_sampler"]
        ev["timing"] = {"backup_started_at": m["started_at"], "backup_finished_at": m["finished_at"],
                        "worldserver_last_seen": smp["worldserver_last_seen"],
                        "mysqld_first_seen_gone": smp["mysqld_first_seen_gone"], "sampler_max_gap_s": smp["max_gap_s"]}
        require(smp["worldserver_last_seen"] and smp["mysqld_first_seen_gone"] and (smp["max_gap_s"] or 9) <= 1.0,
                "the stack sampler didn't see the worldserver running and mysqld stopping", **ev)
        require(m["started_at"] > smp["worldserver_last_seen"] and not m["worldserver_running_at_start"],
                "the backup started before the worldserver exited", **ev)
        require(m["finished_at"] < smp["mysqld_first_seen_gone"], "the backup ended after mysqld stopped", **ev)
        order = backup_stage_order(res["shutdown_events"])
        ev["shutdown_order"] = order
        require(order["in_order"] and order["backup_states"] == ["start", "done"],
                "the shutdown_backup stage isn't start/done between shutdown_login and shutdown_database", **ev)
        require((res["record_backup"] or {}).get("ok") and res["record_backup"].get("id") == m["id"],
                "play-last.json doesn't record this backup", **ev)

        # scenario 2: load each dump into scratch schemas and compare with the live data (= the data at backup time)
        stack.mysql_up()
        from .. import db
        live = live_stats()
        got = {}
        for k in B.DATABASES:
            sch = f"hsbk_chk_{k}"
            loaded.append(sch)
            rec = m["databases"][k]
            db.query(f"DROP DATABASE IF EXISTS `{sch}`; CREATE DATABASE `{sch}` CHARACTER SET {rec['charset']} "
                     f"COLLATE {rec['collation']}", root=True)
            B.load_dump(Path(m["path"]) / rec["file"], sch)
            got[k] = B.table_stats(sch)
        d_live = diff_stats(live, got)
        d_man = diff_stats(manifest_stats(m), got)
        ev["roundtrip"] = {"tables": {k: len(got[k]) for k in got}, "rows": {k: sum(t["rows"] for t in got[k].values())
                                                                          for k in got},
                           "differences_vs_live": d_live[:10], "differences_vs_manifest": d_man[:10]}
        require(all(got[k] for k in got), "a loaded database has no tables", **ev)
        require(not d_live, "a table loaded from the backup differs from the live data (row count or CHECKSUM TABLE)",
                **ev)
        require(not d_man, "a table loaded from the backup differs from the manifest's record", **ev)
        return ev
    finally:
        if loaded:
            from .. import db
            try:
                db.query("".join(f"DROP DATABASE IF EXISTS `{s}`;" for s in loaded), root=True)
            except db.SQLError as e:
                ev["drop_error"] = str(e)
        if sess:
            sess.cleanup()
        if any(stack.stack_processes().values()):
            stack.down()
        drop(root, SCRATCH_CLIENT)


# ---------------------------------------------------------------- backups.rotation

class Watcher(threading.Thread):
    """Lists the folder every 50 ms: complete backups per kind, and temp folders."""

    def __init__(self, root: Path):
        super().__init__(daemon=True, name="backup-watcher")
        self.root = root
        self.samples: list = []
        self.stop_ev = threading.Event()

    def run(self):
        while not self.stop_ev.is_set():
            try:
                ms = B.list_backups(self.root)
                self.samples.append((round(time.time(), 3), sum(m["kind"] == "auto" for m in ms),
                                     sum(m["kind"] == "before-restore" for m in ms), leftovers_in(self.root)))
            except OSError:
                pass
            self.stop_ev.wait(0.05)

    def stop(self):
        self.stop_ev.set()
        self.join(timeout=5)


def verify_rotation(ev: dict) -> dict:
    out = {k: ev[k] for k in ("before", "new_ids", "after", "expected_after", "takes", "watch") if k in ev}
    require(ev["after"] == ev["expected_after"], "the remaining backups aren't exactly the newest 10 automatic and "
            "newest 3 safety backups", **out)
    early = [r for t in ev["takes"] for r in t["rotated"] if r["deleted_at"] < t["completed_at"]]
    out["deleted_before_new_complete"] = early
    require(not early, "a backup was deleted before the new one was complete", **out)
    w = ev["watch"]
    require(w["samples"] >= 20, "the folder watcher took too few samples", **out)
    require(w["min_auto"] >= 10 and w["min_safety"] >= 3,
            "the folder held fewer than 10 automatic / 3 safety backups at some moment (one was deleted before the "
            "new one was complete)", **out)
    require(not ev["leftovers_after"], "temporary folders remain", leftovers=ev["leftovers_after"], **out)
    return out


def rotation_run(root: Path, ev: dict, take=None) -> dict:
    """GIVEN 10 automatic + 3 safety backups; take 2 automatic and 1 safety; record what remains."""
    take = take or B.take
    stack.mysql_up()
    base = B.take("auto", P.TEST_USER, None, root, rotate_after=False)
    now = datetime.now()
    for i in range(1, 10):
        clone_backup(base, root, now - timedelta(hours=i), "auto")
    for i in range(1, 4):
        clone_backup(base, root, now - timedelta(hours=20 + i), "before-restore")
    before = B.list_backups(root)
    ev["before"] = {"auto": sorted(m["id"] for m in before if m["kind"] == "auto"),
                    "before-restore": sorted(m["id"] for m in before if m["kind"] == "before-restore")}
    require(len(ev["before"]["auto"]) == 10 and len(ev["before"]["before-restore"]) == 3,
            "GIVEN not met: the folder doesn't hold 10 automatic and 3 safety backups", **ev)
    w = Watcher(root)
    w.start()
    takes = []
    try:
        for kind in ("auto", "auto", "before-restore"):
            m = take(kind, P.TEST_USER, None, root)
            takes.append({"id": m["id"], "kind": kind, "completed_at": m["completed_at"], "rotated": m["rotated"]})
    finally:
        w.stop()
    ev["takes"] = takes
    ev["new_ids"] = [t["id"] for t in takes]
    after = B.list_backups(root)
    ev["after"] = {"auto": sorted(m["id"] for m in after if m["kind"] == "auto"),
                   "before-restore": sorted(m["id"] for m in after if m["kind"] == "before-restore")}
    every = {k: sorted(ev["before"][k] + [t["id"] for t in takes if t["kind"] == k]) for k in ev["before"]}
    ev["expected_after"] = {"auto": every["auto"][-10:], "before-restore": every["before-restore"][-3:]}
    ev["watch"] = {"samples": len(w.samples), "min_auto": min((s[1] for s in w.samples), default=None),
                   "min_safety": min((s[2] for s in w.samples), default=None),
                   "first": w.samples[:1], "last": w.samples[-1:]}
    ev["leftovers_after"] = leftovers_in(root)
    return ev


@check("backups.rotation", order=366, capability=CAP, stack="down", tier="full", backups=True)
def rotation(ctx):
    """Scratch folder with 10 automatic and 3 safety backups (one real backup plus clones of it under older ids):
    after two more automatic backups and one more safety backup, exactly the newest 10 automatic and newest 3 safety
    remain; no deletion happened before the new backup was complete (deletion times vs completion, and a 50 ms folder
    watcher never saw fewer than 10 automatic / 3 safety backups); no temporary folders remain."""
    ev: dict = {}
    given_clear(ev)
    root = scratch_root("rotation")
    try:
        rotation_run(root, ev)
        return ev | verify_rotation(ev)
    finally:
        stack.down()
        drop(root)


# ---------------------------------------------------------------- backups.failure_nonblocking

def verify_failed_session(res: dict, root: Path, expect: str) -> dict:
    out = {"exit_code": res["exit_code"], "record_backup": res["record_backup"],
           "shutdown_backup": [e for e in res["shutdown_events"] if e["id"] == "shutdown_backup"],
           "remaining_after": res["remaining_after"], "listed": ids(root), "leftovers": leftovers_in(root)}
    require(res["exit_code"] == 0, f"./play exited {res['exit_code']} (a failed backup must not fail shutdown)", **out)
    require(is_clear(res["remaining_after"]), "the stack isn't fully down after a failed backup", **out)
    require(not out["listed"], "a backup is listed although it failed", **out)
    require(not out["leftovers"], "temporary backup files remain", **out)
    rb = res["record_backup"] or {}
    require(rb.get("ok") is False and expect in str(rb.get("error")), f"play-last.json doesn't record the failure "
            f"({expect!r})", **out)
    require(any(e["state"] == "failed" for e in out["shutdown_backup"]), "the shutdown_backup stage isn't 'failed'", **out)
    return out


@check("backups.failure_nonblocking", order=367, capability=CAP, stack="down", tier="full", backups=True)
def failure_nonblocking(ctx):
    """An injected dump failure, then an injected low-disk reading (1 MB free): each session still ends with exit 0
    and the stack fully down, no backup listed, no temporary files, the failure (and why) in play-last.json and the
    shutdown_backup stage 'failed'; the next launch prints a `[notice] Last backup failed ...` line naming that reason
    (the second session for the first failure, a further launch for the second)."""
    ev: dict = {}
    given_clear(ev)
    root = scratch_root("fail")
    sessions = []
    try:
        # 1. dump failure
        s1 = Session("bkfail1", root, knobs={"HS_BACKUP_TEST_FAIL": "dump"})
        sessions.append(s1)
        up = s1.start()
        require(up["up"], f"session 1 never came up: {up.get('why')}", output=s1.s.output()[-2000:], **ev)
        r1 = s1.quit_and_wait()
        ev["dump_failure"] = r1 | {"verify": verify_failed_session(r1, root, "mysqldump")}
        # 2. low disk; its start must carry the notice for failure 1
        s2 = Session("bkfail2", root, knobs={"HS_BACKUP_TEST_FREE_BYTES": str(1 << 20)})
        sessions.append(s2)
        up = s2.start()
        require(up["up"], f"session 2 never came up: {up.get('why')}", output=s2.s.output()[-2000:], **ev)
        r2 = s2.quit_and_wait()
        err1 = r1["record_backup"]["error"]
        ev["low_disk"] = r2 | {"verify": verify_failed_session(r2, root, "not enough free disk space")}
        ev["notice_for_failure_1"] = r2["notices"]
        require(any("Last backup failed" in n and err1 in n for n in r2["notices"]),
                "the next launch didn't print a \"last backup failed\" notice naming the dump failure", **ev)
        # 3. a further launch (fails fast at the world on purpose: nothing to play) carries the low-disk notice
        s3 = Session("bkfail3", root, extra=("--allow-test-client-dir", "--world-datadir", "/nonexistent/hs-bk"))
        sessions.append(s3)
        s3.t_start = time.time()
        s3.s.start_play(*s3.extra, env=s3.env)
        rc3, _ = s3.s.wait_exit(timeout=600)
        r3 = s3.result(rc3)
        err2 = r2["record_backup"]["error"]
        ev["next_launch"] = {k: r3[k] for k in ("exit_code", "notices", "record_backup", "record_outcome", "remaining_after")}
        require(any("Last backup failed" in n and err2 in n for n in r3["notices"]),
                "the next launch didn't print a \"last backup failed\" notice naming the low-disk reason", **ev)
        require(is_clear(r3["remaining_after"]) and not ids(root), "the further launch left something behind", **ev)
        return ev
    finally:
        for s in sessions:
            s.cleanup()
        drop(root)


# ---------------------------------------------------------------- backups.kill_safe

@check("backups.kill_safe", order=368, capability=CAP, stack="down", tier="full", backups=True)
def kill_safe(ctx):
    """An earlier backup in the scratch folder; a session whose backup is paused mid-dump (after the first database,
    test knob) is SIGKILLed: no partial backup is listed, the earlier backup's files still match their checksums
    (and are unchanged), and the next session removes the leftover temp folder, exits 0, adds a complete backup and
    leaves nothing running. Then "Killed in the middle of a restore" (killed_restore): a test restore into scratch
    schemas killed while loading is finished by the next test-shaped ./play; live tables never change."""
    ev: dict = {}
    given_clear(ev)
    root = scratch_root("kill")
    sessions = []
    ok = False
    try:
        stack.mysql_up()
        early = B.take("auto", P.TEST_USER, None, root)
        stack.down()
        early_files = {rel: B.sha256_file(Path(early["path"]) / rel) for rel in early["files"]}
        ev["earlier_backup"] = early["id"]
        s1 = Session("bkkill1", root, knobs={"HS_BACKUP_TEST_PAUSE_S": "240"})
        sessions.append(s1)
        up = s1.start()
        require(up["up"], f"session 1 never came up: {up.get('why')}", output=s1.s.output()[-2000:], **ev)
        s1.s.stop_file.write_text("quit")
        t0, tmp = time.time(), None
        while time.time() - t0 < 300 and s1.s.proc.poll() is None:
            tmp = next((d for d in root.iterdir() if d.name.startswith(".tmp-") and (d / "paused").exists()), None)
            if tmp:
                break
            time.sleep(0.2)
        require(tmp, "GIVEN not met: the backup never reached its mid-dump pause", output=s1.s.output()[-2000:], **ev)
        ev["in_progress"] = {"tmp": tmp.name, "files": sorted(p.name for p in tmp.iterdir()),
                             "play_pid": s1.s.proc.pid}
        os.kill(s1.s.proc.pid, signal.SIGKILL)               # the play loop only (as a crash or force quit would)
        s1.s.proc.wait(timeout=30)
        ev["after_kill"] = {"listed": ids(root), "leftovers": leftovers_in(root), "stack": stack_clear(),
                            "earlier_verify": B.verify(Path(early["path"]))}
        now_files = {rel: B.sha256_file(Path(early["path"]) / rel) for rel in early["files"]}
        require(ev["after_kill"]["listed"] == [early["id"]], "a partial backup is listed after the kill", **ev)
        require(ev["after_kill"]["earlier_verify"]["ok"] and now_files == early_files,
                "the earlier backup's files changed or don't match their checksums", **ev)
        require(tmp.name in ev["after_kill"]["leftovers"], "GIVEN not met: the kill left no temp folder to heal", **ev)
        # the next run heals and works normally
        s2 = Session("bkkill2", root)
        sessions.append(s2)
        up = s2.start()
        ev["next_client_up"] = up
        require(up["up"], f"the next session never came up: {up.get('why')}", output=s2.s.output()[-2000:], **ev)
        ev["leftovers_while_next_runs"] = leftovers_in(root)
        r2 = s2.quit_and_wait()
        listed = B.list_backups(root)
        ev["next"] = {k: r2[k] for k in ("exit_code", "record_backup", "record_backup_plan", "record_lock",
                                         "remaining_after")}
        ev["next"] |= {"listed": [m["id"] for m in listed], "leftovers": leftovers_in(root),
                       "verify": {m["id"]: B.verify(Path(m["path"]), m)["ok"] for m in listed}}
        stale = ((r2["record_backup_plan"] or {}).get("stale_removed") or [])
        require(any(x.get("dir") == tmp.name and x.get("removed") for x in stale),
                "the next run didn't remove the leftover temp folder at its start", **ev)
        require(r2["exit_code"] == 0 and is_clear(r2["remaining_after"]), "the next run didn't end normally", **ev)
        require(len(listed) == 2 and early["id"] in ev["next"]["listed"] and all(ev["next"]["verify"].values())
                and not ev["next"]["leftovers"], "the next run didn't add one complete backup next to the earlier one",
                **ev)
        ev["killed_restore"] = killed_restore(ev)
        ok = True
        return ev
    finally:
        for s in sessions:
            s.cleanup()
        if any(stack.stack_processes().values()):
            stack.down()
        if ok:
            drop(root)


def loader_pids() -> list[int]:
    from .. import procs
    return [pid for pid, cmd in procs.table() if B.LOADER_TAG in cmd and cmd.startswith(str(B.db.MYSQL))]


def killed_restore(ev: dict) -> dict:
    """"Killed in the middle of a restore": a test restore into scratch schemas hsbk_ks_* is SIGKILLed while loading;
    then a test-shaped ./play (scratch folder; its stand-in is missing on purpose, so it stops right after its start
    steps) finishes the restore before anything else: the scratch schemas match the backup, no loader is left, the
    backup and the safety backup verify, and every live table is unchanged (checked before, after the kill, after the
    heal)."""
    out: dict = {}
    root = scratch_root("killrestore")
    prefix = "hsbk_ks"
    ok = False
    try:
        stack.mysql_up()
        live0 = live_stats()
        k = B.take("auto", P.TEST_USER, None, root)
        t = seed_target(k, prefix)
        new_level = (level_in(t) or 1) % 79 + 1
        change_target(t, new_level)               # the scratch "now" differs from the backup being restored
        sfile = root.parent / f"{root.name}-settings.json"
        sfile.write_text(json.dumps({"client": "stock"}))
        env = {**os.environ, **PINNED, "HS_SETTINGS": str(sfile), "HS_BACKUP_DIR": str(root)}
        env.pop("HS_PLAY_CLIENT_DIR", None)
        p = subprocess.Popen([PLAY, "--backups", "restore", k["id"], "--target", prefix, "--json",
                              "--allow-test-client-dir"], cwd=P.ROOT, env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        marker = root / B.INTERRUPTED
        t0 = time.time()
        while time.time() - t0 < 300 and p.poll() is None and not marker.exists():
            time.sleep(0.05)
        require(marker.exists() and p.poll() is None, "GIVEN not met: the restore never started loading", **out)
        time.sleep(3.0)                            # inside the load (characters alone takes ~9 s)
        loaders = loader_pids()
        require(p.poll() is None and loaders, "GIVEN not met: the restore wasn't loading when it was killed",
                loaders=loaders, **out)
        os.kill(p.pid, signal.SIGKILL)
        p.wait(timeout=30)
        out["killed"] = {"pid": p.pid, "loaders_running_at_kill": loaders, "marker": json.loads(marker.read_text())}
        out["live_after_kill"] = diff_stats(live0, live_stats())[:10]
        require(not out["live_after_kill"], "a live table changed during the killed restore", **out)
        safety_id = out["killed"]["marker"]["safety"]
        # the next test-shaped ./play
        r = subprocess.run([PLAY, "--account", "test", "--client-cmd", str(root.parent / "hs-missing-standin.bat"),
                            "--allow-test-client-dir"], cwd=P.ROOT, env=env, capture_output=True, text=True,
                           timeout=600)
        rec = json.loads(play.LAST.read_text()) if play.LAST.exists() else {}
        fin = (rec.get("backup_plan") or {}).get("finished_interrupted_restore") or {}
        out["next_run"] = {"rc": r.returncode, "output": human_lines(r.stdout)[-1500:], "outcome": rec.get("outcome"),
                           "error": rec.get("error"), "finished": {x: fin.get(x) for x in
                                                                   ("backup", "schemas", "killed_loaders", "compare")}}
        require(fin.get("backup") == k["id"] and fin.get("schemas") == t and (fin.get("compare") or {}).get("ok"),
                "the next run didn't finish the interrupted restore", **out)
        require("no such file" in str(rec.get("error")), "the next run didn't get past the heal to its own start",
                **out)
        require(not marker.exists() and not loader_pids(), "the marker or a loader process is left", **out)
        stack.mysql_up()
        out["after_heal"] = {"differences_vs_backup": diff_stats(manifest_stats(k), target_stats(t))[:10],
                             "live_differences": diff_stats(live0, live_stats())[:10],
                             "backup_verify": B.verify(Path(k["path"]), k)["ok"],
                             "safety_verify": B.verify(Path(B.find(root, safety_id)["path"]))["ok"]
                             if B.find(root, safety_id) else False}
        require(not out["after_heal"]["differences_vs_backup"], "the healed scratch schemas don't match the backup", **out)
        require(not out["after_heal"]["live_differences"], "a live table changed", **out)
        require(out["after_heal"]["backup_verify"] and out["after_heal"]["safety_verify"],
                "the backup or its safety backup no longer verifies", **out)
        ok = True
        return out
    except Exception:
        ev["killed_restore"] = out
        raise
    finally:
        if not stack.stack_processes()["mysqld"]:
            stack.mysql_up()
        drop_target(prefix, out)
        stack.down()
        (root.parent / f"{root.name}-settings.json").unlink(missing_ok=True)
        if ok:
            drop(root)


# ---------------------------------------------------------------- backups.player_untouched

GATE_CASES = [
    # (label, argv, environ, expected root: "player" | "none" | "scratch" | "refused")
    ("plain ./play (the player)", [], {}, "player"),
    ("the app (the player)", [], {"HS_FROM_APP": "1", "HS_APP_BUNDLE": "/x/Moonglade.app"}, "player"),
    ("--account test", ["--account", "test"], {}, "none"),
    ("--client-cmd stand-in", ["--client-cmd", "/tmp/x.bat"], {}, "none"),
    ("any check's subprocess ($HS_HARNESS_RUN)", [], {"HS_HARNESS_RUN": "1"}, "none"),
    ("scratch player file", ["--account", "player"], {"HS_PLAYER_FILE": "/tmp/p.json"}, "none"),
    ("scratch settings", [], {"HS_SETTINGS": "/tmp/s.json"}, "none"),
    ("pinned profile", [], {"HS_PROFILE": "m0"}, "none"),
    ("scratch play client + flag", ["--allow-test-client-dir"], {"HS_PLAY_CLIENT_DIR": "/tmp/c"}, "none"),
    ("app.* checks' shape", ["--account", "test", "--client-cmd", "/tmp/x.bat"],
     {"HS_HARNESS_RUN": "1", "HS_PROFILE": "m0", "HS_FROM_APP": "1"}, "none"),
    ("scratch folder + flag", ["--allow-test-client-dir", "--account", "test"], {"HS_BACKUP_DIR": "/tmp/hs-bk"}, "scratch"),
    ("scratch folder, no flag", ["--account", "test"], {"HS_BACKUP_DIR": "/tmp/hs-bk"}, "refused"),
    ("the player's folder + flag", ["--allow-test-client-dir"], {"HS_BACKUP_DIR": str(B.PLAYER_ROOT)}, "refused"),
    ("a folder inside the player's + flag", ["--allow-test-client-dir"], {"HS_BACKUP_DIR": str(B.PLAYER_ROOT / "x")},
     "refused"),
    ("a test knob without a scratch folder", ["--allow-test-client-dir"], {"HS_BACKUP_TEST_FAIL": "dump"}, "refused"),
]


def gate_table() -> list[dict]:
    rows = []
    for label, argv, env, want in GATE_CASES:
        args = play._parse(argv)
        try:
            pl = B.plan(args, env)
            got = ("player" if pl["root"] == B.PLAYER_ROOT else "none" if pl["root"] is None else "scratch")
            row = {"case": label, "want": want, "got": got, "root": str(pl["root"]) if pl["root"] else None}
        except B.Refused as e:
            row = {"case": label, "want": want, "got": "refused", "message": str(e)}
        rows.append(row)
    return rows


@check("backups.player_untouched", order=998, capability=CAP, stack="any", tier="quick", backups=True)
def player_untouched(ctx):
    """The player's env/backups/ is byte-identical (every file's size, mtime, sha256; or still absent) to the start
    of this harness run; the backup gate gives no backup folder to every test-shaped run that names no scratch folder
    (and env/backups/ only to the genuine player launches); and ./play with $HS_BACKUP_DIR but without the test flag
    (and the other misuses) exits nonzero before any server starts (no stack process appears, play-last.json is not
    rewritten); `./play --backups` from a harness-shaped environment without a scratch folder is refused too."""
    start = (ctx or {}).get("player_backups_fingerprint_start", "missing")
    ev: dict = {"player_root": str(B.PLAYER_ROOT)}
    rows = gate_table()
    ev["gate"] = rows
    wrong = [r for r in rows if r["got"] != r["want"]]
    require(not wrong, "the backup gate is wrong for some launch shapes", wrong=wrong, **ev)
    # end to end refusals (before anything starts)
    scratch = scratch_root("refuse")
    try:
        last_before = play.LAST.stat().st_mtime_ns if play.LAST.exists() else None
        stack_before = stack.stack_processes()
        runs = {}
        cases = {
            "HS_BACKUP_DIR without the flag": ([PLAY, "--account", "test", "--client-cmd", "/tmp/hs-none.bat"],
                                                {"HS_BACKUP_DIR": str(scratch)}),
            "HS_BACKUP_DIR = env/backups with the flag": ([PLAY, "--account", "test", "--client-cmd", "/tmp/hs-none.bat",
                                                           "--allow-test-client-dir"],
                                                          {"HS_BACKUP_DIR": str(B.PLAYER_ROOT)}),
            "knob without a scratch folder": ([PLAY, "--account", "test", "--client-cmd", "/tmp/hs-none.bat",
                                               "--allow-test-client-dir"], {"HS_BACKUP_TEST_FREE_BYTES": "1"}),
            "--backups list in a harness-shaped environment": ([PLAY, "--backups", "list"], {}),
            "--backups list, HS_BACKUP_DIR without the flag": ([PLAY, "--backups", "list"], {"HS_BACKUP_DIR": str(scratch)}),
        }
        for name, (cmd, extra) in cases.items():
            env = {**os.environ, **PINNED, "HS_HARNESS_RUN": "1", **extra}
            seen: list = []
            p = subprocess.Popen(cmd, cwd=P.ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            while p.poll() is None:
                seen.append({k: v for k, v in stack.stack_processes().items() if v and v != stack_before[k]})
                time.sleep(0.05)
            out = p.stdout.read()
            new_stack = [x for x in seen if x]
            runs[name] = {"rc": p.returncode, "output": out[-600:], "new_stack_processes": new_stack[:3]}
        ev["refusals"] = runs
        ev["play_last_rewritten"] = (play.LAST.stat().st_mtime_ns if play.LAST.exists() else None) != last_before
        bad = {n: r for n, r in runs.items() if r["rc"] == 0 or r["new_stack_processes"]}
        require(not bad, "a misused backup switch wasn't refused before anything started", bad=bad, **ev)
        require(all(("test switch" in r["output"] or "your own backups folder" in r["output"]
                     or "Refused" in r["output"]) for r in runs.values()),
                "a refusal didn't say why", **ev)
        require(not ev["play_last_rewritten"], "a refused ./play still wrote play-last.json (it got past the start)", **ev)
        require(not scratch.exists() or not any(scratch.iterdir()), "a refused run wrote into the scratch folder", **ev)
    finally:
        drop(scratch)
    now = B.fingerprint(B.PLAYER_ROOT)
    ev |= {"fingerprint_at_run_start": start if start == "missing" or start is None else {"entries": len(start)},
           "fingerprint_now": now if now is None else {"entries": len(now)}}
    require(start != "missing", "the runner didn't record env/backups/ at the start of the run", **ev)
    if start != now:
        changed = sorted(set((start or {})) ^ set((now or {}))) + sorted(
            k for k in set(start or {}) & set(now or {}) if start[k] != now[k])
        require(False, "env/backups/ changed during this harness run", changed=changed[:20], **ev)
    return ev


# ---------------------------------------------------------------- restores (into scratch hsbk_ schemas only)

def target_of(prefix: str) -> dict:
    t = B.scratch_target(prefix)            # raises for anything but scratch hsbk_ names
    assert not B.is_live(t)
    return t


def seed_target(m: dict, prefix: str) -> dict:
    """Load a backup into the scratch schemas <prefix>_* (the test's stand-in for "the databases now")."""
    t = target_of(prefix)
    B.load(m, t)
    return t


def target_stats(t: dict) -> dict:
    return {k: B.table_stats(t[k]) for k in B.DATABASES}


def drop_target(prefix: str, ev: dict) -> None:
    """Drop a check's scratch schemas (mysqld must run). Only hsbk_ names from scratch_target, never protected."""
    from .. import db
    t = target_of(prefix)
    try:
        db.query("".join(f"DROP DATABASE IF EXISTS `{x}`;" for x in t.values()), root=True)
    except db.SQLError as e:
        ev["drop_target_error"] = str(e)


def level_in(t: dict, name: str = P.TEST_CHAR) -> int | None:
    from .. import db
    v = db.scalar(f"SELECT level FROM `{t['characters']}`.characters WHERE name='{name}'", root=True)
    return int(v) if v not in (None, "NULL") else None


def exists_in(t: dict, name: str) -> bool:
    from .. import db
    return db.scalar(f"SELECT COUNT(*) FROM `{t['characters']}`.characters WHERE name='{name}'", root=True) == "1"


def change_target(t: dict, new_level: int) -> None:
    """In the scratch schemas only: the test character's level changes and a new test character appears (a copy of
    its row under a new guid and name)."""
    from .. import db
    ch = f"`{t['characters']}`.characters"
    db.query(f"UPDATE {ch} SET level={int(new_level)} WHERE name='{P.TEST_CHAR}'", root=True)
    cols = [r[0] for r in db.query(f"SELECT column_name FROM information_schema.columns WHERE table_schema="
                                   f"'{t['characters']}' AND table_name='characters' ORDER BY ordinal_position", root=True)]
    guid = int(db.scalar(f"SELECT MAX(guid) FROM {ch}", root=True)) + 1
    sel = [str(guid) if c == "guid" else f"'{NEW_CHAR}'" if c == "name" else f"`{c}`" for c in cols]
    db.query(f"INSERT INTO {ch} ({', '.join(f'`{c}`' for c in cols)}) SELECT {', '.join(sel)} FROM {ch} "
             f"WHERE name='{P.TEST_CHAR}'", root=True)


@check("backups.restore_roundtrip", order=369, capability=CAP, stack="down", tier="full", backups=True)
def restore_roundtrip(ctx):
    """Backup B (of the live databases, read only) is loaded into scratch schemas hsbk_rt_*; there the test character's
    level changes and a new test character appears, and the scratch play client's WTF folder changes.
    `./play --backups restore <B> --target hsbk_rt` (scratch switches) takes a safety backup S of the scratch schemas
    before loading; afterwards every scratch table matches B's manifest (row count + CHECKSUM TABLE), the level is
    back, the new character is gone, the WTF folder matches B, and the stack is down. Restoring S brings back the
    changed level, the new character and S's WTF folder. Every live table's checksum is the same at the end."""
    ev: dict = {}
    given_clear(ev)
    root = scratch_root("restore")
    prefix = "hsbk_rt"
    ok = False
    try:
        ev["scratch_client"] = scratch_client(f"hs-restore-{time.time():.3f}")
        stack.mysql_up()
        live_before = live_stats()
        base = B.take("auto", P.TEST_USER, SCRATCH_CLIENT, root)
        lv = next((c["level"] for c in base["characters"] if c["name"] == P.TEST_CHAR), None)
        require(lv is not None, "GIVEN not met: the backup has no test character", **ev)
        t = seed_target(base, prefix)
        new_level = lv + 1 if lv < 80 else lv - 1
        change_target(t, new_level)
        ev["after_changes"] = {"level": level_in(t), "new_char": exists_in(t, NEW_CHAR)}
        require(ev["after_changes"] == {"level": new_level, "new_char": True},
                "GIVEN not met: the scratch level change / new character didn't happen", **ev)
        stack.down()
        (wtf_dir() / "macros-cache.txt").write_text("MACRO 2 \"changed\"\n/say after B\nEND\n")
        (wtf_dir() / "added-after-B.txt").write_text("x\n")
        wtf_changed = B.wtf_hashes(wtf_dir())

        r1 = backups_cli("restore", base["id"], "--target", prefix, root=root, client=SCRATCH_CLIENT, settings="patched")
        ev["restore_B"] = r1
        js = r1["json"] or {}
        require(r1["rc"] == 0 and js.get("ok"), f"restoring B failed: {js.get('error')}", **ev)
        require(js.get("target") == t, "the restore didn't go to the scratch schemas", **ev)
        listed = {m["id"]: m for m in B.list_backups(root)}
        S_id = (js.get("safety") or {}).get("id")
        S_m = listed.get(S_id)
        ev["safety"] = {"id": S_id, "kind": S_m and S_m["kind"], "completed_at": (js.get("safety") or {}).get("completed_at"),
                        "load_start": js["timeline"].get("load_start"),
                        "characters": S_m and S_m.get("characters")}
        require(S_m and S_m["kind"] == "before-restore", "no safety backup was taken", **ev)
        require(js["safety"]["completed_at"] < js["timeline"]["load_start"], "the safety backup wasn't complete before "
                "the restore started loading", **ev)
        require({m["schema"] for m in S_m["databases"].values()} == set(t.values()),
                "the safety backup isn't of the restore's target", **ev)
        require(not any(stack.stack_processes().values()), "the stack isn't down after the restore", **ev)
        require(manifest_wtf(S_m) == wtf_changed, "the safety backup doesn't hold the WTF folder as it was", **ev)
        stack.mysql_up()
        ev["after_restore_B"] = {"differences_vs_B": diff_stats(manifest_stats(base), target_stats(t))[:10],
                                 "level": level_in(t), "new_char": exists_in(t, NEW_CHAR),
                                 "wtf_matches_B": B.wtf_hashes(wtf_dir()) == manifest_wtf(base)}
        require(not ev["after_restore_B"]["differences_vs_B"], "after the restore the scratch databases don't match B", **ev)
        require(ev["after_restore_B"]["level"] == lv and not ev["after_restore_B"]["new_char"],
                "the level isn't back / the new character still exists", **ev)
        require(ev["after_restore_B"]["wtf_matches_B"], "the WTF account folder doesn't match B", **ev)
        stack.mysql_down()

        r2 = backups_cli("restore", S_id, "--target", prefix, root=root, client=SCRATCH_CLIENT, settings="patched")
        ev["restore_S"] = r2
        require(r2["rc"] == 0 and (r2["json"] or {}).get("ok"), "restoring the safety backup failed", **ev)
        require(not any(stack.stack_processes().values()), "the stack isn't down after restoring S", **ev)
        stack.mysql_up()
        ev["after_restore_S"] = {"differences_vs_S": diff_stats(manifest_stats(S_m), target_stats(t))[:10],
                                 "level": level_in(t), "new_char": exists_in(t, NEW_CHAR),
                                 "wtf_matches_S": B.wtf_hashes(wtf_dir()) == manifest_wtf(S_m)}
        require(not ev["after_restore_S"]["differences_vs_S"], "after restoring S the scratch databases don't match S", **ev)
        require(ev["after_restore_S"]["level"] == new_level and ev["after_restore_S"]["new_char"],
                "restoring S didn't bring back the changed level and the new character", **ev)
        require(ev["after_restore_S"]["wtf_matches_S"], "restoring S didn't bring back the changed WTF folder", **ev)
        ev["live_differences"] = diff_stats(live_before, live_stats())[:10]
        require(not ev["live_differences"], "a live table changed", **ev)
        ok = True
        return ev
    finally:
        if not stack.stack_processes()["mysqld"]:
            stack.mysql_up()
        drop_target(prefix, ev)
        stack.down()
        if ok:                         # a failed run keeps its backups for inspection (env/tmp/)
            drop(root, SCRATCH_CLIENT)


def watch_stack(samples: list, stop: threading.Event) -> threading.Thread:
    def run():
        while not stop.is_set():
            samples.append(stack_clear())
            stop.wait(0.05)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


@check("backups.restore_refusals", order=370, capability=CAP, stack="down", tier="full", backups=True)
def restore_refusals(ctx):
    """(a) A backup with one byte of one dump changed: the restore (into scratch schemas) exits nonzero naming the
    damaged file, takes no safety backup, starts nothing, and no scratch or live table changes. (a2) A test-shaped
    restore naming the live schemas (`--target live`, `--target acore`) and one naming no target are refused with a
    nonzero exit before anything starts, with no safety backup and every live table unchanged. (b) With a play session
    running (the play lock held): the restore exits nonzero with "already running", and the session (client, stack,
    lock) continues unaffected and later exits 0."""
    ev: dict = {}
    given_clear(ev)
    root = scratch_root("refuse2")
    prefix = "hsbk_rf"
    sess = None
    ok = False
    try:
        stack.mysql_up()
        good = B.take("auto", P.TEST_USER, None, root)
        t = seed_target(good, prefix)
        live_before, t_before = live_stats(), target_stats(t)
        stack.down()
        bad = clone_backup(good, root, datetime.now() - timedelta(hours=1), "auto")
        f = Path(bad["path"]) / "auth.sql.gz"
        data = bytearray(f.read_bytes())
        i = len(data) // 2
        data[i] ^= 0x01
        f.unlink()                                          # a clone shares blocks: write a new file, not in place
        f.write_bytes(bytes(data))
        ev["damaged"] = {"id": bad["id"], "file": "auth.sql.gz", "byte": i}
        ids_before = ids(root)
        runs = {}
        for name, args in (("damaged", ("restore", bad["id"], "--target", prefix)),
                           ("live target (--target live)", ("restore", good["id"], "--target", "live")),
                           ("live target (--target acore)", ("restore", good["id"], "--target", "acore")),
                           ("no target", ("restore", good["id"]))):
            samples: list = []
            stop = threading.Event()
            th = watch_stack(samples, stop)
            r = backups_cli(*args, root=root)
            stop.set()
            th.join(timeout=5)
            runs[name] = r | {"stack_seen": [s for s in samples if s][:3], "ids_after": ids(root)}
        ev["refused_runs"] = runs
        for name, r in runs.items():
            js = r["json"] or {}
            require(r["rc"] != 0 and not js.get("ok"), f"{name}: the restore wasn't refused", **ev)
            require(not r["stack_seen"], f"{name}: something was started for the refused restore", **ev)
            require(r["ids_after"] == ids_before, f"{name}: a backup (safety or other) appeared", **ev)
        require("auth.sql.gz" in str((runs["damaged"]["json"] or {}).get("error")),
                "the damaged-backup refusal doesn't name the damaged file", **ev)
        stack.mysql_up()
        ev["damaged_scratch_differences"] = diff_stats(t_before, target_stats(t))[:10]
        ev["live_differences"] = diff_stats(live_before, live_stats())[:10]
        require(not ev["damaged_scratch_differences"], "a scratch table changed although the restore was refused", **ev)
        require(not ev["live_differences"], "a live table changed", **ev)
        stack.down()

        # (b) a running session holds the lock
        sess = Session("bkrefuse", root)
        up = sess.start()
        require(up["up"], f"the session never came up: {up.get('why')}", output=sess.s.output()[-2000:], **ev)
        st_before, lock_before = stack.stack_processes(), play.LOCK.read_text().strip()
        r2 = backups_cli("restore", good["id"], "--target", prefix, root=root)
        js2 = r2["json"] or {}
        time.sleep(2)
        ev["running_restore"] = r2 | {"stack_before": st_before, "stack_after": stack.stack_processes(),
                                      "lock_before": lock_before, "lock_after": play.LOCK.read_text().strip(),
                                      "client_after": play.client_processes(sess.s.bat.name.lower())}
        require(r2["rc"] != 0 and "already running" in str(js2.get("error")), "the restore wasn't refused with "
                "\"already running\"", **ev)
        require(ev["running_restore"]["stack_after"] == st_before and ev["running_restore"]["lock_after"] == lock_before
                == str(sess.s.proc.pid) and ev["running_restore"]["client_after"] and sess.s.proc.poll() is None,
                "the running session was affected", **ev)
        require(not [m for m in B.list_backups(root) if m["kind"] == "before-restore"], "a safety backup was taken", **ev)
        res = sess.quit_and_wait()
        ev["session_end"] = {k: res[k] for k in ("exit_code", "remaining_after", "record_backup")}
        require(res["exit_code"] == 0 and is_clear(res["remaining_after"]), "the session didn't end normally", **ev)
        ok = True
        return ev
    finally:
        if sess:
            sess.cleanup()
        if not stack.stack_processes()["mysqld"]:
            stack.mysql_up()
        drop_target(prefix, ev)
        stack.down()
        if ok:
            drop(root)



# ---------------------------------------------------------------- the app: Backups window, shutdown backup stage

CAP_APP = "mac-app"


def _app_run(root: Path, script: str, tag: str, ev: dict, key: str, go_file: Path | None = None,
             on_go=None, target: str | None = None) -> dict:
    """Open the built app with the Option key injected and a scripted Backups window (test hook). Samples the stack
    and this prefix's Wine processes until the app exits. on_go(run) runs once the app waits for go_file, then
    go_file is created."""
    from .app_checks import (Hook, _log_since, _log_size, app_remaining, open_app, stop_launchers, tcc_prompts,
                             wait_app_exit)
    s = StandIn(tag)
    hook = Hook(s.dir, "hook")
    sfile = s.dir / "settings.json"
    sfile.write_text(json.dumps({"client": "stock"}))
    run: dict = {"script": script}
    ev[key] = run
    seen: list = []
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            st = stack_clear()
            wine = launch.prefix_processes()
            seen.append({"t": round(time.time(), 3), "stack": st, "wine": len(wine)})
            stop.wait(0.2)
    th = threading.Thread(target=watch, daemon=True)
    off, since = _log_size(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        th.start()
        run["t_open"] = round(time.time(), 3)
        run["open"] = open_app(clientbuild_app(), hook, "--account", "test", "--client-cmd", str(s.bat),
                               "--allow-test-client-dir",
                               env={"HS_SETTINGS": str(sfile), "HS_BACKUP_DIR": str(root),
                                    "HS_APP_TEST_OPTION": "1", "HS_APP_TEST_BACKUPS": script,
                                    **({"HS_APP_TEST_RESTORE_TARGET": target} if target else {})})
        require(run["open"]["rc"] == 0, "`open` failed to launch the app", **ev)
        if go_file is not None:
            t0 = time.time()
            while time.time() - t0 < 600 and not [e for e in hook.find("script_step") if e.get("step", "").startswith("wait:")]:
                if hook.exit_code() is not None:
                    break
                time.sleep(0.2)
            run["waiting_seen"] = bool([e for e in hook.find("script_step") if e.get("step", "").startswith("wait:")])
            if run["waiting_seen"] and on_go:
                run["at_wait"] = on_go(run)
            go_file.write_text("go")
            t0 = time.time()
            while time.time() - t0 < 900 and hook.exit_code() is None:
                if s.started.exists() and play.client_processes(s.bat.name.lower()):
                    run["client_up_after_play_s"] = round(time.time() - t0, 1)
                    time.sleep(2)
                    s.stop_file.write_text("quit")
                    break
                time.sleep(0.5)
        rc, dt = wait_app_exit(hook, clientbuild_app(), timeout=600)
        stop.set()
        th.join(timeout=5)
        run |= {"app_exit_code": rc, "events": hook.events(), "samples": len(seen),
                "stack_or_wine_seen": [x for x in seen if x["stack"] or x["wine"]][:5],
                "stand_in_started": s.started.exists(), "remaining_after": app_remaining(s),
                "app_log_tail": _log_since(off)[-2500:]}
        if rc is None and "[app " not in _log_since(off):
            run["tcc_prompts"] = tcc_prompts(since)
        return run
    finally:
        stop.set()
        stop_launchers(clientbuild_app())
        s.cleanup()


def clientbuild_app() -> Path:
    from .. import app as A
    return A.APP


def expected_rows(root: Path) -> list[dict]:
    return [{"id": x["id"], "date": x["date_label"], "kind": x["kind"], "kind_label": x["kind_label"],
             "characters": x["characters_label"]} for x in (B.summary(m) for m in B.list_backups(root))]


def verify_window_shown(run: dict, root_rows: list[dict]) -> dict:
    evs = run["events"]
    shown = [e for e in evs if e.get("kind") == "backups_window" and e.get("state") == "shown"]
    lists = [e for e in evs if e.get("kind") == "backups_list"]
    out = {"shown": shown, "first_list": lists[:1], "expected_rows": root_rows,
           "option_launch": [e for e in evs if e.get("kind") == "option_launch"]}
    require(shown and shown[0].get("visible") and shown[0].get("onscreen"), "the Backups window wasn't shown on screen",
            **out)
    require(lists and lists[0].get("ok") and lists[0].get("rows") == root_rows,
            "the Backups window doesn't list the folder's backups newest first with their date, kind and characters",
            **out)
    return out


@check("app.backups_window", order=386, capability=CAP_APP, stack="down", tier="full", backups=True)
def backups_window(ctx):
    """Built app, scratch folder with 3 backups (one of the live databases, one of scratch schemas where the test
    character is a level higher, and a clone as a safety backup), Option reported held (test hook): (1) the Backups
    window lists the 3 newest first with date, kind and characters, nothing of the stack, Wine or the client starts,
    and closing it exits 0 with nothing running; (2) choosing Restore and cancelling the confirmation takes no safety
    backup and changes no table; (3) choosing the older backup and confirming (restore target: scratch schemas
    hsbk_app_*, through the test hook): the confirmation names its date and the safety backup, a safety backup
    appears, every scratch table then matches the chosen backup's manifest and no live table changed (checked while
    the app waits), and Play starts the normal loading flow with the stand-in client; the app exits 0 afterwards."""
    from .app_checks import build_app, require_clear
    ev: dict = {}
    build_app(ev)
    require_clear(ev)
    root = scratch_root("appwin")
    go = root.parent / f"{root.name}-go"
    prefix = "hsbk_app"
    ok = False
    try:
        stack.mysql_up()
        live_before = live_stats()
        old = B.take("auto", P.TEST_USER, None, root)                     # live, read only
        lv = next(c["level"] for c in old["characters"] if c["name"] == P.TEST_CHAR)
        t = seed_target(old, prefix)
        from .. import db
        db.query(f"UPDATE `{t['characters']}`.characters SET level={lv % 79 + 1} WHERE name='{P.TEST_CHAR}'", root=True)
        newer = B.take("auto", P.TEST_USER, None, root, schemas=t)          # the scratch "now", a level higher
        clone_backup(newer, root, datetime.now() - timedelta(days=1), "before-restore")
        t_before = target_stats(t)
        stack.down()
        rows = expected_rows(root)
        ev["given_rows"] = rows
        require(len(rows) == 3 and [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)
                and rows[0]["characters"] != rows[1]["characters"],
                "GIVEN not met: the folder doesn't hold 3 backups with different levels", **ev)

        # (1) list, start nothing, close
        go1 = root.parent / f"{root.name}-go1"         # the window stays open ~3 s while the stack is sampled
        r1 = _app_run(root, f"wait:{go1};close", "bkapp1", ev, "list_and_close", target=prefix, go_file=go1,
                      on_go=lambda run: time.sleep(3) or {"held_open_s": 3})
        go1.unlink(missing_ok=True)
        out1 = verify_window_shown(r1, rows)
        closed = [e for e in r1["events"] if e.get("kind") == "backups_window" and e.get("state") == "closed"]
        ev["list_and_close_verdict"] = out1 | {"closed": closed}
        require(r1.get("waiting_seen") and not r1["stack_or_wine_seen"] and not r1["stand_in_started"]
                and r1["samples"] >= 5,
                "something was started while the Backups window was open", **ev)
        require(closed and closed[0].get("why") == "closed" and r1["app_exit_code"] == 0,
                "closing the Backups window didn't exit the app with 0", **ev)
        require(is_clear(r1["remaining_after"]), "something is running after the app exited", **ev)

        # (2) restore, cancel
        target = old["id"]
        r2 = _app_run(root, f"select:{target};restore;cancel;close", "bkapp2", ev, "cancel", target=prefix)
        conf = [e for e in r2["events"] if e.get("kind") == "confirm"]
        stack.mysql_up()
        ev["cancel_verdict"] = {"confirm": conf, "rows_after": expected_rows(root) == rows,
                                "scratch_differences": diff_stats(t_before, target_stats(t))[:10],
                                "live_differences": diff_stats(live_before, live_stats())[:10],
                                "restore_events": [e for e in r2["events"] if e.get("kind") == "restore"]}
        stack.down()
        require(conf and conf[0].get("answer") == "cancel" and conf[0].get("id") == target,
                "GIVEN not met: the confirmation wasn't shown and cancelled", **ev)
        require(ev["cancel_verdict"]["rows_after"] and not ev["cancel_verdict"]["restore_events"],
                "cancelling still restored or took a backup", **ev)
        require(not ev["cancel_verdict"]["scratch_differences"] and not ev["cancel_verdict"]["live_differences"],
                "a table changed after cancelling", **ev)
        require(r2["app_exit_code"] == 0 and is_clear(r2["remaining_after"]), "the cancel run didn't end cleanly", **ev)

        # (3) restore, confirm, play
        def at_wait(run):
            stack.mysql_up()
            try:
                return {"differences_vs_chosen": diff_stats(manifest_stats(old), target_stats(t))[:10],
                        "level": level_in(t), "live_differences": diff_stats(live_before, live_stats())[:10],
                        "listed": ids(root)}
            finally:
                stack.down()
        r3 = _app_run(root, f"select:{target};restore;confirm;wait:{go};play", "bkapp3", ev, "restore_play",
                      go_file=go, on_go=at_wait, target=prefix)
        conf = [e for e in r3["events"] if e.get("kind") == "confirm"]
        rest = [e for e in r3["events"] if e.get("kind") == "restore" and e.get("state") == "done"]
        stages = [e for e in r3["events"] if e.get("kind") == "stage"]
        aw = r3.get("at_wait") or {}
        new_safety = [i for i in aw.get("listed", []) if i.endswith("-before-restore") and i not in [r["id"] for r in rows]]
        ev["restore_verdict"] = {"confirm": conf, "restore_done": rest, "at_wait": aw, "new_safety": new_safety,
                                 "stage_ids": list(dict.fromkeys(e.get("id") for e in stages)),
                                 "client_up_after_play_s": r3.get("client_up_after_play_s")}
        date = next(r["date"] for r in rows if r["id"] == target)
        require(conf and conf[0].get("answer") == "restore" and date in conf[0].get("text", "") + conf[0].get("title", "")
                and "safety backup" in conf[0].get("text", ""),
                "the confirmation doesn't name the backup's date and the safety backup", **ev)
        require(rest and rest[0].get("ok") and rest[0].get("safety") in new_safety,
                "the restore didn't run, or no safety backup appeared", **ev)
        require(r3.get("waiting_seen") and not aw.get("differences_vs_chosen") and aw.get("level") == lv,
                "after the restore the scratch databases don't match the chosen backup", **ev)
        require(not aw.get("live_differences"), "a live table changed during the app's restore", **ev)
        require("launching" in ev["restore_verdict"]["stage_ids"] and r3.get("client_up_after_play_s") is not None,
                "Play didn't start the normal loading flow and the stand-in client", **ev)
        require(r3["app_exit_code"] == 0 and is_clear(r3["remaining_after"]), "the play run didn't end cleanly", **ev)
        ok = True
        return ev
    finally:
        go.unlink(missing_ok=True)
        if stack.stack_processes()["worldserver"] or stack.stack_processes()["authserver"]:
            stack.down()
        if not stack.stack_processes()["mysqld"]:
            stack.mysql_up()
        drop_target(prefix, ev)
        stack.down()
        if ok:
            drop(root)



def backup_stage_app_run(ev: dict) -> dict:
    """save-backups: the app runs the game (stand-in, m0) with a scratch backup folder and the game exits by itself;
    the hook's shutdown events and the scratch folder are the evidence."""
    from .app_checks import (Hook, _log_size, app_remaining, open_app, stop_launchers, wait_app_client_up,
                             wait_app_exit)
    root = scratch_root("appstage")
    s = StandIn("bkstage")
    hook = Hook(s.dir, "hook")
    sfile = s.dir / "settings.json"
    sfile.write_text(json.dumps({"client": "stock"}))
    run: dict = {"root": str(root)}
    ev["backup_stage_run"] = run
    off, since = _log_size(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        run["t_open"] = t0 = round(time.time(), 3)
        run["open"] = open_app(clientbuild_app(), hook, "--account", "test", "--client-cmd", str(s.bat),
                               "--allow-test-client-dir", env={"HS_SETTINGS": str(sfile), "HS_BACKUP_DIR": str(root)})
        up = wait_app_client_up(s, hook, off, since)
        run["client_up"] = up
        require(up["up"], f"stand-in client never came up under the app: {up.get('why')}", **ev)
        run["listed_before_exit"] = ids(root)
        s.stop_file.write_text("quit")                    # the game exits by itself
        rc, dt = wait_app_exit(hook, clientbuild_app())
        listed = B.list_backups(root)
        run |= {"app_exit_code": rc, "events": hook.events(), "remaining_after": app_remaining(s),
                "listed_after": [{"id": m["id"], "started_at": m["started_at"], "kind": m["kind"]} for m in listed],
                "t0": t0}
        return run
    finally:
        stop_launchers(clientbuild_app())
        s.cleanup()
        drop(root)


def verify_backup_stage(run: dict) -> dict:
    """The backup stage is shown in order: shutdown_backup start+done after shutdown_login and before
    shutdown_database, the shutdown window visible and on screen at those events, a new backup in the scratch folder,
    the app exits 0 and nothing remains."""
    evs = run["events"]
    st = [e for e in evs if e.get("kind") == "shutdown_stage"]
    order = backup_stage_order(st)
    bk = [e for e in st if e.get("id") == "shutdown_backup"]
    shown = [e for e in evs if e.get("kind") == "shutdown_window" and e.get("state") == "shown"]
    out = {"shutdown_order": order, "backup_events": bk, "shown": shown, "listed_before_exit": run["listed_before_exit"],
           "listed_after": run["listed_after"], "app_exit_code": run["app_exit_code"],
           "remaining_after": run["remaining_after"]}
    require(shown and shown[0].get("visible") and shown[0].get("onscreen"), "no shutdown window was shown", **out)
    require(order["in_order"] and order["backup_states"] == ["start", "done"],
            "shutdown_backup isn't recorded (start, done) after shutdown_login and before shutdown_database", **out)
    require(all(e.get("visible") and e.get("onscreen") and e.get("shutdown_mode") for e in bk),
            "the shutdown window wasn't on screen during the backup stage", **out)
    new = [m for m in run["listed_after"] if m["id"] not in run["listed_before_exit"] and m["started_at"] >= run["t0"]]
    require(not run["listed_before_exit"] and len(new) == 1 and new[0]["kind"] == "auto",
            "no (single) new backup exists in the scratch folder", **out)
    require(run["app_exit_code"] == 0 and is_clear(run["remaining_after"]), "the app didn't exit 0 with nothing left",
            **out)
    return out
