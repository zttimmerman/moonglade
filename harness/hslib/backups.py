"""Save backups (openspec/changes/save-backups): a backup on every quit, the last 10 kept, verified restore.

What a backup is (Zach, 2026-10-06: "whole save"): the acore_characters, acore_auth and acore_playerbots databases
plus the play client's per-account UI folder WTF/Account/<ACCOUNT>/ (key bindings, macros, chat/addon settings).
The world database (game content, vanilla-mode data) is never part of a backup.

Layout (env/backups/ for the player; a scratch folder for tests):

    <root>/YYYYMMDD-HHMMSS-<kind>/        kind: auto (taken on quit) | before-restore (safety backup)
        manifest.json                    id, kind, times, pins, account, characters, per-file sha256, per-table stats
        characters.sql.gz, auth.sql.gz, playerbots.sql.gz
        wtf/                             a copy of WTF/Account/<ACCOUNT>/ (absent if the client had none)
    <root>/.tmp-<id>/                    a backup being written (never listed; removed by every entry point)
    <root>/.del-<id>/                    a backup being rotated out (never listed; removed by every entry point)
    <root>/.last-backup.json             the last automatic backup's outcome (the "last backup failed" notice)

Dump format (design.md "As built"): per database, one gzip stream of
    1. mysqldump --no-data --routines --events --skip-triggers     (schema)
    2. mysqldump --no-create-info --hex-blob, minus FLOAT tables    (data)
    3. our own INSERTs for tables with FLOAT/DOUBLE columns         (data, exact)
    4. mysqldump --no-create-info --no-data --triggers              (triggers, after the data)
mysqldump prints FLOAT columns with 6 significant digits (the server's text protocol), so a reload moves e.g.
characters.position_x by up to ~0.005 and CHECKSUM TABLE changes (measured 2026-10-06: characters,
character_homebind, corpse). Section 3 selects those columns as CAST(col AS DOUBLE), which is exact, and every
string as hex, so the reload is byte-identical (backups.on_quit compares CHECKSUM TABLE for every table).

Consistency: a backup is taken with the world and login servers stopped (take() refuses otherwise), so nothing
writes while the database is dumped. Atomic: everything is written into .tmp-<id>/, fsynced, re-hashed against the
manifest, then renamed to <id>/; rotation runs only after that rename.

Test switches (refused by ./play unless --allow-test-client-dir is given, honoured only for a scratch folder):
    HS_BACKUP_DIR                 the backup folder to use instead of env/backups/ (never env/backups/ itself)
    HS_BACKUP_TEST_FAIL=dump      the second database's dump fails (failure_nonblocking)
    HS_BACKUP_TEST_FREE_BYTES=N   pretend the disk has N bytes free (the low-disk guard)
    HS_BACKUP_TEST_PAUSE_S=N      pause N s after the first database is written (kill_safe: SIGKILL mid-backup)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db, paths as P

PLAYER_ROOT = P.PLAYER_BACKUPS
TEST_ROOT_ENV = "HS_BACKUP_DIR"
TEST_FAIL_ENV = "HS_BACKUP_TEST_FAIL"
TEST_FREE_ENV = "HS_BACKUP_TEST_FREE_BYTES"
TEST_PAUSE_ENV = "HS_BACKUP_TEST_PAUSE_S"
TEST_KNOB_ENVS = (TEST_FAIL_ENV, TEST_FREE_ENV, TEST_PAUSE_ENV)
DATABASES = ("characters", "auth", "playerbots")      # keys of P.DB_NAMES; never "world"
KEEP = {"auto": 10, "before-restore": 3}              # Zach, 2026-10-06
KIND_LABEL = {"auto": "Automatic (on quit)", "before-restore": "Before restore (safety)"}
MIN_FREE_BYTES = 2 * 2**30                            # design.md: skip with a recorded failure below 2 GB free
STATUS = ".last-backup.json"
FORMAT = 1
MYSQLDUMP = P.MYSQLDUMP          # dev: Homebrew's; release: the payload's
GZIP = "/usr/bin/gzip"
_ID_RE = re.compile(r"^\d{8}-\d{6}-(auto|before-restore)$")
INSERT_CHUNK = 1 << 20                                # bytes of row literals per INSERT statement


class BackupError(RuntimeError):
    pass


class Refused(RuntimeError):
    """A test switch used where it isn't allowed (./play exits nonzero before anything starts)."""


# ---------------------------------------------------------------- where backups go (the gate)

def is_player_root(root: Path) -> bool:
    r, p = Path(root).resolve(), PLAYER_ROOT.resolve()
    return r == p or p in r.parents or r in p.parents


def plan(args, environ=None) -> dict:
    """Whether this ./play takes a backup when it ends, and where. Raises Refused for a misused test switch.

    - $HS_BACKUP_DIR set: only with --allow-test-client-dir (the explicit test flag), never pointing at (or inside,
      or above) env/backups/; that scratch folder is used, with the test knobs.
    - otherwise a genuine player launch (no harness sign, play.harness_signs): env/backups/, no knobs.
    - otherwise (a test-shaped run that names no scratch folder): no backup."""
    from . import play
    env = os.environ if environ is None else environ
    flag = bool(getattr(args, "allow_test_client_dir", False))
    knobs = {k: env[k] for k in TEST_KNOB_ENVS if env.get(k)}
    if env.get(TEST_ROOT_ENV):
        if not flag:
            raise Refused(f"{TEST_ROOT_ENV} is set: that's a test switch for the harness, not for playing. "
                          f"Unset it (`unset {TEST_ROOT_ENV}`) and run ./play again.")
        root = Path(env[TEST_ROOT_ENV]).resolve()
        if is_player_root(root):
            raise Refused(f"{TEST_ROOT_ENV} points at your own backups folder ({PLAYER_ROOT}); tests use a scratch "
                          "folder")
        return {"root": root, "test": True, "knobs": knobs}
    if knobs:
        raise Refused(f"{sorted(knobs)[0]} is set: that's a test switch for the harness, not for playing. Unset it "
                      "and run ./play again.")
    if P.MODE == "release" and P.release_switch(P.RELEASE_HOME_ENV, env):
        # release-first-launch: a scratch HOME (test switch, honoured only with the test flag inside a live harness
        # run) is the run's own, and so is its backups folder: a test-shaped run backs up there as a player's would
        return {"root": PLAYER_ROOT, "test": True, "knobs": {}, "why": "release scratch HOME (test switch)"}
    signs = play.harness_signs(args, env)
    if signs:
        return {"root": None, "test": True, "knobs": {}, "why": "not a genuine player launch: " + ", ".join(signs)}
    return {"root": PLAYER_ROOT, "test": False, "knobs": {}}


# ---------------------------------------------------------------- helpers

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_path(p: Path) -> None:
    fd = os.open(p, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def free_bytes(root: Path, knobs: dict | None = None) -> int:
    if knobs and knobs.get(TEST_FREE_ENV):
        return int(knobs[TEST_FREE_ENV])
    d = Path(root)
    while not d.exists():
        d = d.parent
    return shutil.disk_usage(d).free


def _schema(key: str) -> str:
    return P.DB_NAMES[key]


LIVE = {k: P.DB_NAMES[k] for k in DATABASES}           # the player's databases
SCRATCH_RE = re.compile(r"^hsbk_[a-z0-9]+(?:_[a-z0-9]+)*$")
PROTECTED = {"hsbk_t", "acore_characters_broken"}       # Zach's recovery copies (2026-10-06): never a target


def scratch_target(prefix: str) -> dict:
    """A test restore's target: scratch schemas <prefix>_characters / _auth / _playerbots (prefix starts hsbk_).
    Raises Refused for anything else (the live acore_* schemas above all)."""
    if not prefix or not SCRATCH_RE.match(prefix) or prefix in PROTECTED:
        raise Refused(f"a test restore needs a scratch target (--target hsbk_<name>), not {prefix!r}")
    t = {k: f"{prefix}_{k}" for k in DATABASES}
    bad = [x for x in t.values() if x in PROTECTED or x in LIVE.values() or not x.startswith("hsbk_")]
    if bad:
        raise Refused(f"{bad} can't be a test restore target")
    return t


def is_live(target: dict) -> bool:
    return any(not str(x).startswith("hsbk_") for x in target.values())


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "''")


def _cnf() -> str:
    return f"--defaults-file={db.client_cnf(root=True)}"


def _proc_cmd(pid: int) -> str:
    from . import procs
    for p, cmd in procs.table():
        if p == pid:
            return cmd
    return ""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    from . import procs
    return not procs.is_zombie(pid)


# ---------------------------------------------------------------- stale temp folders (kill-safe)

def clean_stale(root: Path) -> list[dict]:
    """Remove .tmp-*/.del-* left by a killed backup or rotation. A .tmp whose writer is still alive (a running
    take()) is left alone; a dead writer's leftover dump processes (mysqldump/gzip/mysql, recorded in its
    `children` file) are killed first."""
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not d.name.startswith((".tmp-", ".del-")):
            continue
        owner = None
        try:
            owner = json.loads((d / "owner.json").read_text()).get("pid")
        except (OSError, ValueError):
            pass
        if d.name.startswith(".tmp-") and owner and owner != os.getpid() and _alive(owner) \
                and "hslib" in _proc_cmd(owner):
            out.append({"dir": d.name, "left": "its writer (pid %d) is still running" % owner})
            continue
        killed = []
        try:
            kids = [int(x) for x in (d / "children").read_text().split()]
        except (OSError, ValueError):
            kids = []
        for k in kids:
            cmd = _proc_cmd(k)
            if _alive(k) and any(w in cmd for w in ("mysqldump", "gzip", "mysql")):
                try:
                    os.kill(k, signal.SIGKILL)
                    killed.append(k)
                except ProcessLookupError:
                    pass
        shutil.rmtree(d, ignore_errors=True)
        out.append({"dir": d.name, "removed": not d.exists(), "killed_children": killed})
    return out


# ---------------------------------------------------------------- listing

def read_manifest(d: Path) -> dict | None:
    """The manifest of a complete backup folder, or None (temp folders, junk, unreadable manifests)."""
    d = Path(d)
    if not d.is_dir() or not _ID_RE.match(d.name):
        return None
    try:
        m = json.loads((d / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or m.get("id") != d.name or m.get("kind") not in KEEP \
            or not isinstance(m.get("files"), dict) or set(m.get("databases") or {}) != set(DATABASES):
        return None
    return m


def list_backups(root: Path) -> list[dict]:
    """Every complete backup in root, newest first (ids sort by time)."""
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        m = read_manifest(d)
        if m:
            out.append(m | {"path": str(d)})
    return sorted(out, key=lambda m: m["id"], reverse=True)


def find(root: Path, backup_id: str) -> dict | None:
    return next((m for m in list_backups(root) if m["id"] == backup_id), None)


def verify(d: Path, manifest: dict | None = None) -> dict:
    """Re-hash every file the manifest lists. {"ok", "bad": [{"file", "why"}], "checked"}; IO errors count as bad."""
    d = Path(d)
    m = manifest if manifest is not None else read_manifest(d)
    if not m:
        return {"ok": False, "bad": [{"file": "manifest.json", "why": "missing or invalid"}], "checked": 0}
    bad = []
    for rel, rec in sorted(m["files"].items()):
        f = d / rel
        try:
            got = sha256_file(f)
        except OSError as e:
            bad.append({"file": rel, "why": f"unreadable: {e}"})
            continue
        if got != rec.get("sha256"):
            bad.append({"file": rel, "why": "checksum mismatch", "expected": rec.get("sha256"), "actual": got})
    # files that appeared next to the manifest's are not trusted either
    extra = sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()
                   and str(p.relative_to(d)) not in m["files"] and p.name != "manifest.json")
    for rel in extra:
        bad.append({"file": rel, "why": "not in the manifest"})
    return {"ok": not bad, "bad": bad, "checked": len(m["files"])}


# ---------------------------------------------------------------- database stats

def base_tables(schema: str) -> list[str]:
    return [r[0] for r in db.query(f"SELECT table_name FROM information_schema.tables WHERE table_schema='{_q(schema)}' "
                                   "AND table_type='BASE TABLE' ORDER BY table_name", root=True)]


def table_stats(schema: str) -> dict:
    """{table: {"rows": n, "checksum": "..."}} for every base table (CHECKSUM TABLE ... EXTENDED). Raises on any SQL
    error (callers treat that as a failure, never as "no tables")."""
    names = base_tables(schema)
    if not names:
        return {}
    ck = db.query("CHECKSUM TABLE " + ", ".join(f"`{schema}`.`{n}`" for n in names) + " EXTENDED", root=True,
                  timeout=600)
    sums = {r[0].split(".", 1)[1]: r[1] for r in ck}
    counts = db.query(" UNION ALL ".join(f"SELECT '{_q(n)}', COUNT(*) FROM `{schema}`.`{n}`" for n in names),
                      root=True, timeout=600)
    cnt = {r[0]: int(r[1]) for r in counts}
    missing = [n for n in names if n not in sums or n not in cnt or sums[n] in ("NULL", "")]
    if missing:
        raise BackupError(f"couldn't checksum {schema} tables {missing[:5]}")
    return {n: {"rows": cnt[n], "checksum": sums[n]} for n in names}


def schema_charset(schema: str) -> tuple[str, str]:
    rows = db.query(f"SELECT default_character_set_name, default_collation_name FROM information_schema.schemata "
                    f"WHERE schema_name='{_q(schema)}'", root=True)
    if not rows:
        raise BackupError(f"database {schema} doesn't exist")
    return rows[0][0], rows[0][1]


def characters(account: str, schemas: dict | None = None) -> list[dict]:
    """The account's characters: name, level, class (id + name from ChrClasses.dbc), race."""
    sc = schemas or LIVE
    rows = db.query(f"SELECT c.guid, c.name, c.level, c.class, c.race FROM `{sc['characters']}`.characters c "
                    f"JOIN `{sc['auth']}`.account a ON a.id = c.account "
                    f"WHERE a.username = '{_q(account.upper())}' ORDER BY c.guid", root=True)
    try:
        from .dbc import class_names
        names = class_names()
    except (OSError, AssertionError, ValueError):
        names = {}
    return [{"guid": int(g), "name": n, "level": int(lv), "class": int(c), "class_name": names.get(int(c)),
             "race": int(r)} for g, n, lv, c, r in rows]


# ---------------------------------------------------------------- dump

def _float_tables(schema: str) -> list[str]:
    return [r[0] for r in db.query(
        "SELECT DISTINCT c.table_name FROM information_schema.columns c JOIN information_schema.tables t "
        "ON t.table_schema=c.table_schema AND t.table_name=c.table_name AND t.table_type='BASE TABLE' "
        f"WHERE c.table_schema='{_q(schema)}' AND c.data_type IN ('float','double') ORDER BY 1", root=True)]


_INT = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "decimal", "numeric", "year"}
_TEXT = {"char", "varchar", "tinytext", "text", "mediumtext", "longtext", "enum", "set", "json"}
_BIN = {"binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"}
_TIME = {"date", "datetime", "timestamp", "time"}


def _col_expr(name: str, dtype: str, charset: str | None) -> str:
    c = f"`{name}`"
    if dtype in _INT:
        v = f"CAST({c} AS CHAR)"
    elif dtype in ("float", "double"):
        v = f"CAST(CAST({c} AS DOUBLE) AS CHAR)"          # exact: a FLOAT widened to DOUBLE prints all its digits
    elif dtype == "bit":
        v = f"CAST({c} + 0 AS CHAR)"
    elif dtype in _TEXT:
        v = f"CONCAT('_{charset or 'utf8mb4'} X''', HEX({c}), '''')"
    elif dtype in _BIN:
        v = f"CONCAT('X''', HEX({c}), '''')"
    elif dtype in _TIME:
        v = f"QUOTE(CAST({c} AS CHAR))"
    else:
        raise BackupError(f"column {name}: type {dtype} isn't handled by the exact dumper")
    return f"IF({c} IS NULL, 'NULL', {v})"


def _exact_rows_sql(schema: str, table: str) -> tuple[str, list[str]]:
    cols = db.query(f"SELECT column_name, data_type, character_set_name, generation_expression FROM "
                    f"information_schema.columns WHERE table_schema='{_q(schema)}' AND table_name='{_q(table)}' "
                    "ORDER BY ordinal_position", root=True)
    use = [(n, t.lower(), None if cs == "NULL" else cs) for n, t, cs, gen in cols if gen in ("", "NULL")]
    pk = [r[0] for r in db.query(f"SELECT column_name FROM information_schema.key_column_usage WHERE "
                                 f"table_schema='{_q(schema)}' AND table_name='{_q(table)}' AND "
                                 "constraint_name='PRIMARY' ORDER BY ordinal_position", root=True)]
    exprs = ", ".join(_col_expr(n, t, cs) for n, t, cs in use)
    order = (" ORDER BY " + ", ".join(f"`{k}`" for k in pk)) if pk else ""
    sql = (f"SET SESSION time_zone='+00:00';\n"
           f"SELECT CONCAT('(', CONCAT_WS(',', {exprs}), ')') FROM `{schema}`.`{table}`{order};\n")
    return sql, [n for n, _t, _cs in use]


def _write_exact(schema: str, tables: list[str], out, add_child) -> dict:
    """Section 3: INSERTs for tables with FLOAT/DOUBLE columns, every value exact."""
    out.write(b"\n-- moonglade exact section (FLOAT tables)\n/*!40101 SET NAMES utf8mb4 */;\n"
              b"SET @hs_sql_mode=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\n"
              b"SET @hs_tz=@@TIME_ZONE, TIME_ZONE='+00:00';\n"
              b"SET @hs_fk=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0;\n"
              b"SET @hs_uc=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0;\n")
    rows_total = {}
    for t in tables:
        sql, cols = _exact_rows_sql(schema, t)
        head = (f"INSERT INTO `{t}` (" + ",".join(f"`{c}`" for c in cols) + ") VALUES ").encode()
        p = subprocess.Popen([str(db.MYSQL), _cnf(), "--batch", "--raw", "--skip-column-names", "--quick"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        add_child(p.pid)
        p.stdin.write(sql.encode())
        p.stdin.close()
        buf, size, n = [], 0, 0
        for line in p.stdout:
            row = line.rstrip(b"\n")
            buf.append(row)
            size += len(row) + 1
            n += 1
            if size >= INSERT_CHUNK:
                out.write(head + b",".join(buf) + b";\n")
                buf, size = [], 0
        if buf:
            out.write(head + b",".join(buf) + b";\n")
        err = p.stderr.read().decode(errors="replace")
        if p.wait() != 0:
            raise BackupError(f"exact dump of {schema}.{t} failed: {err.strip()[-300:]}")
        rows_total[t] = n
    out.write(b"SET SQL_MODE=@hs_sql_mode; SET TIME_ZONE=@hs_tz; SET FOREIGN_KEY_CHECKS=@hs_fk; "
              b"SET UNIQUE_CHECKS=@hs_uc;\n")
    return rows_total


def dump_database(schema: str, dest: Path, add_child, fail: bool = False) -> dict:
    """Write one database's dump (4 sections, design.md "As built") as gzip to dest. Raises BackupError."""
    floats = _float_tables(schema)
    base = [str(MYSQLDUMP), _cnf(), "--single-transaction", "--set-gtid-purged=OFF", "--skip-dump-date",
            "--hex-blob", "--default-character-set=utf8mb4"]
    sections = [
        base + ["--no-data", "--routines", "--events", "--skip-triggers", schema],
        base + ["--no-create-info", "--skip-triggers", *[f"--ignore-table={schema}.{t}" for t in floats], schema],
        None,                                                     # exact section
        base + ["--no-create-info", "--no-data", "--triggers", "--skip-routines", schema],
    ]
    with open(dest, "wb") as fh:
        gz = subprocess.Popen([GZIP, "-6", "-c"], stdin=subprocess.PIPE, stdout=fh, stderr=subprocess.PIPE)
        add_child(gz.pid)
        exact_rows = {}
        try:
            for i, cmd in enumerate(sections):
                if cmd is None:
                    exact_rows = _write_exact(schema, floats, gz.stdin, add_child)
                    gz.stdin.flush()          # our writes are buffered; mysqldump writes the pipe directly
                    continue
                gz.stdin.flush()
                if fail and i == 1:
                    cmd = cmd[:-1] + [schema + "_hs_injected_failure"]       # test knob: a dump that fails
                p = subprocess.Popen(cmd, stdout=gz.stdin, stderr=subprocess.PIPE)
                add_child(p.pid)
                err = p.stderr.read().decode(errors="replace")
                if p.wait() != 0:
                    raise BackupError(f"mysqldump of {schema} failed (section {i + 1}): {err.strip()[-300:]}")
        finally:
            try:
                gz.stdin.close()
            except BrokenPipeError:
                pass
            gz_err = gz.stderr.read().decode(errors="replace")
            gz_rc = gz.wait()
        if gz_rc != 0:
            raise BackupError(f"gzip failed for {schema}: {gz_err.strip()[-300:]}")
        fh.flush()
        os.fsync(fh.fileno())
    return {"schema": schema, "float_tables": floats, "exact_rows": exact_rows}


# ---------------------------------------------------------------- take

def _new_id(root: Path, kind: str) -> str:
    while True:
        bid = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{kind}"
        if not (root / bid).exists() and not (root / f".tmp-{bid}").exists():
            return bid
        time.sleep(0.2)


def _copy_wtf(client_dir: Path | None, account: str, dest: Path) -> dict:
    if client_dir is None:
        return {"present": False, "why": "no client folder"}
    src = Path(client_dir) / "WTF" / "Account" / account.upper()
    info = {"source": str(src)}
    if not src.is_dir():
        return info | {"present": False}
    shutil.copytree(src, dest, symlinks=True)
    return info | {"present": True}


def take(kind: str, account: str, client_dir: Path | None, root: Path, knobs: dict | None = None,
         rotate_after: bool = True, schemas: dict | None = None) -> dict:
    """Take a backup (world and login servers stopped, mysqld running). Returns the manifest plus "path",
    "rotated" and "completed_at". Raises BackupError; nothing partial is ever listed."""
    from . import stack
    knobs = knobs or {}
    schemas = schemas or LIVE                 # a test restore's safety backup dumps its scratch target instead
    if kind not in KEEP:
        raise BackupError(f"unknown backup kind {kind}")
    t_start = time.time()
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    stale = clean_stale(root)
    procs_now = stack.stack_processes()
    if procs_now["worldserver"] or procs_now["authserver"]:
        raise BackupError("the world or login server is still running; a backup needs them stopped")
    if not procs_now["mysqld"] or not stack.mysql_ready():
        raise BackupError("the database isn't running")
    free = free_bytes(root, knobs)
    if free < MIN_FREE_BYTES:
        raise BackupError(f"not enough free disk space ({free / 2**30:.1f} GB free, a backup needs 2 GB)")
    bid = _new_id(root, kind)
    tmp = root / f".tmp-{bid}"
    tmp.mkdir()
    (tmp / "owner.json").write_text(json.dumps({"pid": os.getpid(), "started": _now_iso()}))

    def add_child(pid: int) -> None:
        with open(tmp / "children", "a") as f:
            f.write(f"{pid}\n")
    try:
        man: dict = {"format": FORMAT, "id": bid, "kind": kind, "created": _now_iso(),
                     "started_at": round(t_start, 3), "account": account.upper(),
                     "worldserver_running_at_start": bool(procs_now["worldserver"]),
                     "mysqld_pids": procs_now["mysqld"],
                     "core_sha": P.fork_lock().get("core_sha"), "playerbots_sha": P.fork_lock().get("playerbots_sha"),
                     "stale_removed": stale, "databases": {}, "files": {}}
        man["characters"] = characters(account, schemas)
        for i, key in enumerate(DATABASES):
            schema = schemas[key]
            cs, coll = schema_charset(schema)
            fname = f"{key}.sql.gz"
            t0 = time.time()
            info = dump_database(schema, tmp / fname, add_child, fail=knobs.get(TEST_FAIL_ENV) == "dump" and i == 1)
            man["databases"][key] = {"schema": schema, "file": fname, "charset": cs, "collation": coll,
                                     "dump_s": round(time.time() - t0, 2), **info, "tables": table_stats(schema)}
            if i == 0 and knobs.get(TEST_PAUSE_ENV):
                (tmp / "paused").write_text(str(time.time()))
                time.sleep(float(knobs[TEST_PAUSE_ENV]))
        man["wtf"] = _copy_wtf(client_dir, account, tmp / "wtf")
        junk = ("owner.json", "children", "paused")          # bookkeeping of a backup in progress, not backup data
        for p in sorted(tmp.rglob("*")):
            if p.is_file() and str(p.relative_to(tmp)) not in junk:
                rel = str(p.relative_to(tmp))
                man["files"][rel] = {"sha256": sha256_file(p), "bytes": p.stat().st_size}
                _fsync_path(p)
        man["bytes"] = sum(f["bytes"] for f in man["files"].values())
        for j in junk:
            (tmp / j).unlink(missing_ok=True)
        man["finished_at"] = round(time.time(), 3)
        man["duration_s"] = round(man["finished_at"] - t_start, 2)
        (tmp / "manifest.json").write_text(json.dumps(man, indent=2))
        _fsync_path(tmp / "manifest.json")
        check = verify(tmp, man)                              # re-hash what is on disk before it can be listed
        if not check["ok"]:
            raise BackupError(f"the backup didn't verify after writing: {check['bad'][:3]}")
        _fsync_path(tmp)
        os.rename(tmp, root / bid)
        _fsync_path(root)
        completed = round(time.time(), 3)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    rotated = rotate(root) if rotate_after else []
    return man | {"path": str(root / bid), "completed_at": completed, "rotated": rotated}


def rotate(root: Path) -> list[dict]:
    """Delete the oldest backups beyond KEEP per kind (renamed to .del-<id> first, so a kill never leaves a
    half-deleted backup listed). Returns what was deleted, with times."""
    out = []
    by_kind: dict = {}
    for m in list_backups(root):
        by_kind.setdefault(m["kind"], []).append(m["id"])
    for kind, ids in by_kind.items():
        for bid in sorted(ids, reverse=True)[KEEP[kind]:]:
            src, gone = Path(root) / bid, Path(root) / f".del-{bid}"
            os.rename(src, gone)
            shutil.rmtree(gone, ignore_errors=True)
            out.append({"id": bid, "kind": kind, "deleted_at": round(time.time(), 3)})
    return out


# ---------------------------------------------------------------- the last automatic backup's outcome

def write_status(root: Path, rec: dict) -> None:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{STATUS}.tmp"
    tmp.write_text(json.dumps(rec, indent=2))
    os.replace(tmp, root / STATUS)


def read_status(root: Path) -> dict | None:
    try:
        d = json.loads((Path(root) / STATUS).read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def failure_notice(root: Path | None) -> str | None:
    """The "last backup failed" notice for the next launch, or None."""
    if root is None:
        return None
    st = read_status(root)
    if not st or st.get("ok") is not False:
        return None
    when = st.get("at_local") or st.get("at") or "?"
    return f"Last backup failed ({when}): {st.get('error')}. Your earlier backups are kept."


def on_quit(root: Path, account: str, client_dir: Path | None, knobs: dict | None) -> dict:
    """The play loop's backup (stack.down's before_database hook). Never raises: a failure is recorded."""
    t0 = time.time()
    try:
        m = take("auto", account, client_dir, root, knobs)
        rec = {"ok": True, "id": m["id"], "path": m["path"], "bytes": m["bytes"], "started_at": m["started_at"],
               "finished_at": m["finished_at"], "duration_s": m["duration_s"], "rotated": m["rotated"],
               "characters": [{k: c[k] for k in ("name", "level", "class_name")} for c in m["characters"]]}
    except Exception as e:  # noqa: BLE001
        rec = {"ok": False, "error": f"{e}" if isinstance(e, BackupError) else f"{type(e).__name__}: {e}",
               "duration_s": round(time.time() - t0, 2)}
        try:
            clean_stale(root)
        except OSError:
            pass
    rec |= {"root": str(root), "at": _now_iso(), "at_local": datetime.now().strftime("%Y-%m-%d %H:%M")}
    try:
        write_status(root, {k: rec[k] for k in ("ok", "at", "at_local", "root") if k in rec}
                     | ({"error": rec["error"]} if not rec["ok"] else {"id": rec["id"]}))
    except OSError as e:
        rec["status_write_error"] = str(e)
    return rec


# ---------------------------------------------------------------- restore

def client_dir_for(env: dict, test: bool) -> Path | None:
    """Where the player's WTF folder lives for this ./play (same selection as the play loop: $HS_PLAY_CLIENT_DIR /
    $HS_CLIENT_DIR scratch copies for tests, else the client setting: env/client-play/ or client/)."""
    from . import clientbuild, settings
    if env.get("HS_PLAY_CLIENT_DIR"):
        return Path(env["HS_PLAY_CLIENT_DIR"]).resolve()
    if env.get("HS_CLIENT_DIR"):
        return Path(env["HS_CLIENT_DIR"]).resolve()
    if test:
        return None                      # a test restore never writes the player's own client
    return clientbuild.PLAY if settings.client_mode()["mode"] == "patched" else P.CLIENT


def load(m: dict, target: dict) -> dict:
    """Replace the target's three databases with the backup's dumps (mysqld running; world/login stopped)."""
    d = Path(m["path"])
    out = {}
    for key in DATABASES:
        rec = m["databases"][key]
        schema = target[key]
        if schema in PROTECTED:
            raise BackupError(f"{schema} is protected")
        t0 = time.time()
        db.query(f"DROP DATABASE IF EXISTS `{schema}`; CREATE DATABASE `{schema}` CHARACTER SET {rec['charset']} "
                 f"COLLATE {rec['collation']};", root=True, timeout=600)
        load_dump(d / rec["file"], schema)
        out[key] = {"load_s": round(time.time() - t0, 2)}
    return out


LOADER_TAG = "--max-allowed-packet=64M"        # only load_dump's mysql runs with this (orphan detection)


def kill_orphan_loaders() -> list[int]:
    """A killed restore's dump loaders (gzip -dc | mysql ... <schema>) keep running; stop them before reloading."""
    from . import procs
    out = []
    for pid, cmd in procs.table():
        if cmd.startswith(str(db.MYSQL)) and LOADER_TAG in cmd and pid != os.getpid():
            try:
                os.kill(pid, signal.SIGKILL)
                out.append(pid)
            except ProcessLookupError:
                pass
    deadline = time.time() + 10
    while any(_alive(p) for p in out) and time.time() < deadline:
        time.sleep(0.1)
    return out


def load_dump(f: Path, schema: str) -> None:
    """Pipe one gzip dump into mysql for `schema`. Raises BackupError."""
    with open(f, "rb") as src:
        gz = subprocess.Popen([GZIP, "-dc"], stdin=src, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        my = subprocess.Popen([str(db.MYSQL), _cnf(), LOADER_TAG, schema], stdin=gz.stdout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        gz.stdout.close()
        my_out, my_err = my.communicate()
        gz_err = gz.stderr.read()
        gz_rc = gz.wait()
    if my.returncode != 0 or gz_rc != 0:
        raise BackupError(f"loading {f.name} into {schema} failed: "
                          f"{(my_err or gz_err).decode(errors='replace').strip()[-300:]}")


def compare_live(m: dict, target: dict) -> dict:
    """The target databases' tables vs the manifest's stats. {"ok", "diff": [...]} (SQL errors raise)."""
    diff = []
    for key in DATABASES:
        want = m["databases"][key]["tables"]
        have = table_stats(target[key])
        for t in sorted(set(want) | set(have)):
            if want.get(t) != have.get(t):
                diff.append({"db": key, "table": t, "backup": want.get(t), "live": have.get(t)})
    return {"ok": not diff, "diff": diff[:20], "differences": len(diff)}


def replace_wtf(m: dict, client_dir: Path | None) -> dict:
    """Make client_dir/WTF/Account/<ACCOUNT>/ what the backup holds (removed if the backup had none)."""
    if client_dir is None:
        return {"skipped": "no client folder for this restore"}
    acct = Path(client_dir) / "WTF" / "Account" / m["account"]
    src = Path(m["path"]) / "wtf"
    new, old = acct.with_name(acct.name + ".hs-restore-new"), acct.with_name(acct.name + ".hs-restore-old")
    for x in (new, old):
        shutil.rmtree(x, ignore_errors=True)
    if (m.get("wtf") or {}).get("present"):
        acct.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, new, symlinks=True)
    if acct.exists():
        os.rename(acct, old)
    if new.exists():
        os.rename(new, acct)
    shutil.rmtree(old, ignore_errors=True)
    return {"folder": str(acct), "present": acct.is_dir()}


def wtf_hashes(folder: Path) -> dict:
    folder = Path(folder)
    if not folder.is_dir():
        return {}
    return {str(p.relative_to(folder)): sha256_file(p) for p in sorted(folder.rglob("*")) if p.is_file()}


INTERRUPTED = ".restore-in-progress.json"


def finish_interrupted(root: Path, client_dir: Path | None, say=lambda s: None) -> dict | None:
    """Kill-safe restore: a restore killed while loading leaves INTERRUPTED (target + safety ids) and half-loaded
    databases. The next entry point (./play, a restore; the caller holds the play lock, the world and login servers
    are stopped) loads the target backup again, checks every table, puts its WTF folder back and drops the marker.
    None when there is nothing to finish; raises BackupError when it can't be finished (the game must not start)."""
    from . import stack
    f = Path(root) / INTERRUPTED
    if not f.exists():
        return None
    try:
        rec = json.loads(f.read_text())
    except (OSError, ValueError) as e:
        raise BackupError(f"{f} is unreadable ({e}); restore a backup by hand") from None
    m = find(root, rec.get("backup", ""))
    target = rec.get("schemas")
    if not isinstance(target, dict) or set(target) != set(DATABASES):
        raise BackupError(f"{f} names no target databases; restore a backup by hand")
    if (is_live(target) and not is_player_root(root)) or any(x in PROTECTED for x in target.values()):
        raise BackupError(f"{f} (a test folder) names live or protected databases {target}; refusing to load them")
    if not m or not verify(Path(m["path"]), m)["ok"]:
        raise BackupError(f"a restore of {rec.get('backup')} was interrupted and that backup is missing or damaged; "
                          f"restore the safety backup {rec.get('safety')} with ./play --backups restore")
    st = stack.stack_processes()
    if st["worldserver"] or st["authserver"]:
        raise BackupError("a restore was interrupted, but the game server is running; stop it and try again")
    killed = kill_orphan_loaders()
    say(f"Finishing an interrupted restore of the backup from {label_time(m)}...")
    started = not st["mysqld"]
    try:
        if started:
            stack.mysql_up()
        out = {"backup": m["id"], "schemas": target, "safety": rec.get("safety"), "killed_loaders": killed,
               "load": load(m, target), "compare": compare_live(m, target)}
        if not out["compare"]["ok"]:
            raise BackupError(f"finishing the interrupted restore of {m['id']} left "
                              f"{out['compare']['differences']} table(s) different")
        out["wtf"] = replace_wtf(m, client_dir)
        f.unlink()
        return out
    finally:
        if started:
            stack.mysql_down()
            stack.PIDFILE["mysqld"].unlink(missing_ok=True)


def restore(backup_id: str, root: Path, client_dir: Path | None, say=print, target: dict | None = None,
            allow_live: bool = False) -> dict:
    """Restore a backup: refuse while Moonglade runs or if the backup is damaged (nothing changed); otherwise start
    mysqld only, take a before-restore safety backup, load the dumps, check every table against the manifest
    (rolling back to the safety backup if that fails), replace the WTF account folder, stop mysqld. Returns a
    result dict with "ok"; "code" is the exit code."""
    from . import play, stack
    target = target or LIVE
    res: dict = {"ok": False, "id": backup_id, "root": str(root), "target": target, "timeline": {}}
    tl = res["timeline"]
    if is_live(target) and not allow_live:            # only a genuine player restore may touch the live databases
        return res | {"code": 1, "error": "a test restore can't target the live databases; nothing was touched"}
    if any(x in PROTECTED for x in target.values()):
        return res | {"code": 1, "error": f"{target} includes a protected schema; nothing was touched"}

    def mark(k):
        tl[k] = round(time.time(), 3)
    try:
        fd, _lock = play.acquire_lock()
    except play.Busy as b:
        return res | {"code": 2, "error": f"Moonglade is already running (pid {b.pid}). Quit the game first, then "
                                          "restore."}
    started_mysql = False
    try:
        mark("locked")
        res["stale_removed"] = clean_stale(root)
        try:
            res["finished_interrupted"] = finish_interrupted(root, client_dir, say)
        except BackupError as e:
            return res | {"code": 1, "error": str(e)}
        m = find(root, backup_id)
        if not m:
            return res | {"code": 1, "error": f"no backup named {backup_id} in {root}"}
        v = verify(Path(m["path"]), m)
        res["verify"] = v
        if not v["ok"]:
            names = ", ".join(sorted({b["file"] for b in v["bad"]}))
            return res | {"code": 1, "error": f"backup {backup_id} is damaged ({names} doesn't match its checksum); "
                                              "nothing was changed"}
        mark("verified")
        st = stack.stack_processes()
        if st["worldserver"] or st["authserver"]:
            return res | {"code": 1, "error": "the game server is running; quit Moonglade (or `harness/hs down`) "
                                              "and restore again"}
        if not st["mysqld"]:
            say("Starting the database...")
            stack.mysql_up()
            started_mysql = True
        mark("mysqld_up")
        missing = [x for x in target.values() if not db.scalar(
            f"SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='{_q(x)}'", root=True) == "1"]
        if missing:
            return res | {"code": 1, "error": f"the target database(s) {missing} don't exist; nothing was changed"}
        say("Saving a safety backup of how things are now...")
        safety = take("before-restore", m["account"], client_dir, root, schemas=target)
        res["safety"] = {"id": safety["id"], "path": safety["path"], "completed_at": safety["completed_at"],
                         "rotated": safety["rotated"]}
        mark("safety_done")
        say(f"Restoring the backup from {label_time(m)}...")
        marker = Path(root) / INTERRUPTED
        marker.write_text(json.dumps({"backup": m["id"], "schemas": target, "safety": safety["id"],
                                      "at": _now_iso()}))
        _fsync_path(marker)
        try:
            mark("load_start")
            res["load"] = load(m, target)
            res["compare"] = compare_live(m, target)
            if not res["compare"]["ok"]:
                raise BackupError(f"after loading, {res['compare']['differences']} table(s) don't match the backup")
            mark("load_done")
        except Exception as e:  # noqa: BLE001
            res["load_error"] = str(e)
            say(f"The restore failed ({e}); putting things back from the safety backup...")
            try:
                load(find(root, safety["id"]), target)
                res["rollback_compare"] = compare_live(find(root, safety["id"]), target)
                res["rolled_back"] = res["rollback_compare"]["ok"]
                if res["rolled_back"]:
                    marker.unlink(missing_ok=True)
            except Exception as e2:  # noqa: BLE001
                res["rolled_back"] = False
                res["rollback_error"] = str(e2)
            return res | {"code": 1, "error": f"the restore failed: {e}" + (
                "; everything was put back as it was" if res.get("rolled_back") else
                f"; putting things back ALSO failed, restore the safety backup {safety['id']} by hand")}
        res["wtf"] = replace_wtf(m, client_dir)
        marker.unlink(missing_ok=True)
        mark("wtf_done")
        res |= {"ok": True, "code": 0, "restored": {"id": m["id"], "kind": m["kind"], "created": m["created"],
                                                    "characters": m.get("characters")}}
        return res
    finally:
        if started_mysql:
            res["mysqld_down"] = stack.mysql_down()
            stack.PIDFILE["mysqld"].unlink(missing_ok=True)
        mark("finished")
        play.release_lock(fd)


def fingerprint(root: Path) -> dict | None:
    """Every file and folder under root with size, mtime and sha256 (None when root doesn't exist): the harness
    compares the player's env/backups/ at the start and end of every run (backups.player_untouched)."""
    root = Path(root)
    if not root.exists():
        return None
    out = {".": {"is_dir": root.is_dir(), "mtime_ns": root.stat().st_mtime_ns}}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        rel = str(p.relative_to(root))
        out[rel] = ({"dir": True, "mtime_ns": st.st_mtime_ns} if p.is_dir() and not p.is_symlink() else
                    {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns,
                     "sha256": sha256_file(p) if p.is_file() and not p.is_symlink() else None})
    return out


# ---------------------------------------------------------------- presentation

def label_time(m: dict) -> str:
    try:
        t = datetime.strptime(m["id"][:15], "%Y%m%d-%H%M%S")
        return t.strftime("%a %-d %b %Y, %H:%M:%S")
    except ValueError:
        return m["id"]


def summary(m: dict) -> dict:
    """One backup as the Backups window and `--backups list --json` show it."""
    chars = m.get("characters") or []
    return {"id": m["id"], "kind": m["kind"], "kind_label": KIND_LABEL[m["kind"]], "created": m.get("created"),
            "date_label": label_time(m), "account": m.get("account"), "bytes": m.get("bytes"),
            "characters": [{"name": c["name"], "level": c["level"], "class": c.get("class_name") or c.get("class")}
                           for c in chars],
            "characters_label": ", ".join(f"{c['name']} (level {c['level']}"
                                          + (f" {c['class_name']})" if c.get("class_name") else ")")
                                          for c in chars) or "no characters"}


def cli(argv: list[str]) -> int:
    """`./play --backups [list | restore <id>] [--json]`."""
    import argparse
    ap = argparse.ArgumentParser(prog="./play --backups", description="List your save backups, or restore one.")
    ap.add_argument("action", nargs="?", default="list", choices=["list", "restore"])
    ap.add_argument("id", nargs="?")
    ap.add_argument("--json", action="store_true", help="machine-readable output (the app's Backups window)")
    ap.add_argument("--allow-test-client-dir", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--target", help=argparse.SUPPRESS)   # test-only: scratch schema prefix (hsbk_...) to restore into
    args = ap.parse_args(argv)
    env = os.environ
    as_json = args.json

    def emit(obj: dict, text: str | None = None) -> None:
        if as_json:
            print(json.dumps(obj, indent=2), flush=True)
        elif text:
            print(text, flush=True)

    for var in ("HS_PLAY_CLIENT_DIR", "HS_CLIENT_DIR", *TEST_KNOB_ENVS):
        if env.get(var) and not args.allow_test_client_dir:
            emit({"ok": False, "error": f"{var} is a test switch"},
                 f"{var} is set: that's a test switch for the harness. Unset it and try again.")
            return 1
    try:
        pl = plan(args, env)
    except Refused as e:
        emit({"ok": False, "error": str(e)}, str(e))
        return 1
    if pl["root"] is None:              # a test-shaped run that names no scratch folder: never the player's backups
        emit({"ok": False, "error": pl["why"]}, f"Refused: this is a test run ({pl['why']}); tests name a scratch "
             f"backup folder with {TEST_ROOT_ENV}.")
        return 1
    root = pl["root"]
    if args.action == "list":
        stale = clean_stale(root)
        items = [summary(m) for m in list_backups(root)]
        notice = failure_notice(root)
        if as_json:
            emit({"ok": True, "root": str(root), "backups": items, "last_failure": notice, "stale_removed": stale})
        else:
            if notice:
                print(f"[notice] {notice}")
            if not items:
                print(f"No backups yet in {root}. One is taken every time you quit the game.")
            for b in items:
                print(f"{b['id']}  {b['date_label']:<28} {b['kind_label']:<26} {b['characters_label']}")
            if items:
                print("\nTo restore one: ./play --backups restore <id>")
        return 0
    if not args.id:
        emit({"ok": False, "error": "restore needs a backup id"}, "Usage: ./play --backups restore <id>")
        return 1
    # where the restore goes: a genuine player restore -> the live databases; a test restore -> scratch hsbk_ schemas
    # named by --target, refused (before anything is touched) without one or with anything else
    if pl["test"]:
        try:
            target = scratch_target(args.target or "")
        except Refused as e:
            emit({"ok": False, "error": f"{e}; nothing was touched"}, f"Restore refused: {e}; nothing was touched.")
            return 1
    else:
        if args.target:
            emit({"ok": False, "error": "--target is a test switch"}, "--target is a test switch for the harness.")
            return 1
        target = LIVE
    cdir = client_dir_for(env, pl["test"])
    if pl["test"] and cdir is not None:
        from . import clientbuild
        if cdir.resolve() in (clientbuild.PLAY.resolve(), P.CLIENT.resolve()):
            emit({"ok": False, "error": "a test restore can't write the player's own client"},
                 "A test restore can't write the player's own client.")
            return 1
    say = (lambda s: None) if as_json else (lambda s: print(f"[backups] {s}", flush=True))
    res = restore(args.id, root, cdir, say=say, target=target, allow_live=not pl["test"])
    if as_json:
        emit(res)
    elif res["ok"]:
        print(f"Restored the backup from {label_time(res['restored'] | {'id': res['id']})}. A safety backup of how "
              f"things were is {res['safety']['id']} (restore it to undo).")
    else:
        print(f"Restore refused: {res['error']}")
    return res.get("code", 1)
