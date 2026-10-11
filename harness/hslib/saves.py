"""Driver for the saves checks (openspec/changes/front-menu-saves; checks/saves_checks.py).

Never the PLAYER account and never env/saves/<PLAYER>: everything here runs on the harness's own saves account
P.SAVES_USER (security level 3, see GM_LEVEL, so its characters can type GM commands on themselves:
.levelup, .modify money, .additem, .quest add, .setskill, .learn, .modify reputation, .pet create, .go). Its characters:
  P.SAVES_CHAR  a dwarf hunter (race 3, class 3: SharedDefines.h Races / Classes), the round-trip character with a pet;
  P.SAVES_ALT   a human warrior, the second character (saves.scope) and the playerbot companion of saves.bots_skip.

The headless probe talks to mod-moonglade exactly as the Moonglade add-on does: an add-on whisper to the character's
own name, prefix "MGSAVE" (CMSG_MESSAGECHAT, LANG_ADDON); the server answers with add-on whispers "MGSAVE\\t...".
The launcher side uses SOAP: `.moonglade save take|load|status|list|tables|test|interval`.

Shared database rule (hs-change-loop checklist 11): before the first load of a harness process, safety_dump() writes a
mysqldump of the characters database into a run-scoped folder under env/tmp/ (never env/backups); only the latest is
kept, and its path goes into the evidence. player_rows() fingerprints the PLAYER account's characters' save-set rows
(saves.player_untouched compares the start of the run with its end).

Every world-data choice (creatures, bankers, talents, skills, factions, quests, items) is made by a rule over the world
database or the client's DBCs, written next to it, never from memory (CLAUDE.md §7)."""
from __future__ import annotations

import contextlib
import threading

import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import time
from pathlib import Path

from . import db, dbc, fixtures as F, paths as P, soap
from .probe import client as probe
from .probe import proto as W

GM_LEVEL = 3                     # SEC_ADMINISTRATOR: role 192, which alone links RBAC_PERM_COMMAND_MAILBOX (777, via role
                                 # 196; rbac_linked_permissions) for mail_item; it includes level 2's GM commands
RACE_DWARF, CLASS_HUNTER = 3, 3  # SharedDefines.h enum Races / enum Classes
LANG_ADDON = 0xFFFFFFFF          # SharedDefines.h Language::LANG_ADDON
PREFIX = "MGSAVE"
HEADER = P.MODULES / "mod-moonglade" / "src" / "MoongladeSaves.h"
START_TELE = "ElwynnForest"      # a game_tele row in the human start zone (fixtures.tele_row validates it)

# probe packets (Opcodes.h) and their layouts (the WorldSession handlers named)
CMSG_DESTROYITEM = 0x111            # ItemHandler.cpp HandleDestroyItemOpcode: u8 bag, u8 slot, u32 count
CMSG_QUESTLOG_REMOVE_QUEST = 0x194  # QuestHandler.cpp HandleQuestLogRemoveQuest: u8 slot
CMSG_BANKER_ACTIVATE = 0x1B7        # NPCHandler.cpp HandleBankerActivateOpcode: u64 guid
CMSG_AUTOBANK_ITEM = 0x283          # ItemHandler.cpp HandleAutoBankItemOpcode: u8 bag, u8 slot
CMSG_EQUIPMENT_SET_SAVE = 0x4BD     # ItemHandler.cpp HandleEquipmentSetSave: packed set guid, u32 index, name, icon,
                                    # 19 packed item guids
CMSG_LEARN_TALENT = 0x251          # TalentHandler.cpp HandleLearnTalentOpcode: u32 talent id, u32 rank (0 = first)
CMSG_ACTIVATETAXI = 0x1AD           # TaxiHandler.cpp HandleActivateTaxiOpcode: u64 npc guid, u32 from, u32 to
INVENTORY_SLOT_BAG_0 = 255          # Player.h: the backpack's "bag"
INVENTORY_SLOT_ITEM_START = 23      # Player.h: the first backpack slot
EQUIPMENT_SLOT_END = 19             # Player.h
UNIT_NPC_FLAG_BANKER = 0x20000      # UnitDefines.h NPCFlags
UNIT_NPC_FLAG_FLIGHTMASTER = 0x2000
CREATURE_TYPE_BEAST, CREATURE_TYPE_FLAG_TAMEABLE = 1, 0x1   # SharedDefines.h
PLAYER_FIELD_COINAGE = W.UNIT_END + 0x03FE                   # UpdateFields.h
PLAYER_QUEST_LOG_1_1 = W.UNIT_END + 0x000A                   # UpdateFields.h (5 fields per quest log slot)


class Identity:
    """Whose account and characters the saves helpers use (test-pyramid 1.2 prototype: per-test characters). The
    default is the fixed saves account (P.SAVES_USER with P.SAVES_CHAR and P.SAVES_ALT); a shared-server test runs
    under its own with `as_identity`, per thread, so two pooled tests never share a character."""
    def __init__(self, user, password, char, alt):
        self.user, self.password, self.char, self.alt = user, password, char, alt


_IDENT = threading.local()


def ident() -> Identity:
    return getattr(_IDENT, "v", None) or Identity(P.SAVES_USER, P.SAVES_PASS, P.SAVES_CHAR, P.SAVES_ALT)


@contextlib.contextmanager
def as_identity(i: Identity):
    prev = getattr(_IDENT, "v", None)
    _IDENT.v = i
    try:
        yield i
    finally:
        _IDENT.v = prev


class SavesError(Exception):
    def __init__(self, msg: str, **evidence):
        super().__init__(msg)
        self.evidence = evidence


def gm(cmd: str, timeout: float = 20) -> str:
    return soap.gm(cmd, timeout=timeout).output.strip()


# ----------------------------------------------------------------------------- the module's save set (one source)

def save_set() -> list[dict]:
    """MoongladeSaves.h SAVE_SET: [{name, where, renumber: {column: kind}}] in order; REMOVED_ON_LOAD and KEPT."""
    text = HEADER.read_text()
    block = text[text.index("SAVE_SET[] ="):text.index("REMOVED_ON_LOAD[] =")]
    out = []
    for m in re.finditer(r'\{\s*"(\w+)",\s*"([^"]*)",\s*\{(.*?)\}\s*\},', block, re.S):
        ren = dict(re.findall(r'\{\s*"(\w+)",\s*Renumber::(\w+)\s*\}', m.group(3)))
        out.append({"name": m.group(1), "where": m.group(2), "renumber": ren})
    return out


def classification() -> dict:
    text = HEADER.read_text()
    removed = re.findall(r'\{\s*"(\w+)",\s*"[^"]*"\s*\}', text[text.index("REMOVED_ON_LOAD[] ="):text.index("KEPT[] =")])
    kept = re.findall(r'"(\w+)"', text[text.index("KEPT[] ="):])
    return {"in_save": [t["name"] for t in save_set()], "removed_on_load": removed, "kept": kept}


def columns(table: str) -> list[tuple[str, str]]:
    return [(c, t) for c, t in db.query(
        f"SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
        f"AND TABLE_NAME = '{table}' ORDER BY ORDINAL_POSITION", "characters")]


def _select(cols: list[tuple[str, str]]) -> str:
    """The module's text form of every column (MoongladeSaves.cpp SelectList)."""
    out = []
    for c, t in cols:
        if t in ("blob", "tinyblob", "mediumblob", "longblob", "binary", "varbinary", "bit"):
            out.append(f"HEX(`{c}`)")
        elif t in ("float", "double", "decimal"):
            out.append(f"CAST(CAST(`{c}` AS DOUBLE) AS CHAR)")
        else:
            out.append(f"CAST(`{c}` AS CHAR)")
    return ", ".join(out)


def db_rows(guid: int) -> dict:
    """The character's save-set rows as the database holds them now: {table: {"columns", "rows"}} (NULL -> None)."""
    out = {}
    for t in save_set():
        cols = columns(t["name"])
        where = t["where"].replace("{g}", str(int(guid)))
        rows = db.query(f"SELECT {_select(cols)} FROM `{t['name']}` WHERE {where}", "characters")
        out[t["name"]] = {"columns": [f"{c} {ty}" for c, ty in cols],
                          "rows": [[None if v == "NULL" else v for v in r] for r in rows]}
    return out


def file_rows(save_dir: Path) -> dict:
    """A save's character.json.gz as the same shape as db_rows()."""
    d = json.loads(gzip.decompress((Path(save_dir) / "character.json.gz").read_bytes()))
    return {t: {"columns": d["schema"][t], "rows": d["rows"][t]} for t in d["rows"]}


def normalize(rows: dict, ignore: dict | None = None) -> dict:
    """Content form for comparison: item, pet and equipment-set ids (which a load renumbers) are replaced by the
    content they name, and every table becomes a sorted list. `ignore` = {table: [columns]} left out."""
    spec = {t["name"]: t["renumber"] for t in save_set()}
    ignore = ignore or {}

    def col(table, name):
        return [c.split(" ")[0] for c in rows[table]["columns"]].index(name)

    items, pets, sets = {}, {}, {}
    ii = rows["item_instance"]
    for r in ii["rows"]:
        g = r[col("item_instance", "guid")]
        items[g] = "item:" + "|".join("" if v is None else v for i, v in enumerate(r) if i != col("item_instance", "guid"))
    for r in rows["character_pet"]["rows"]:
        g = r[col("character_pet", "id")]
        pets[g] = "pet:" + "|".join("" if v is None else v for i, v in enumerate(r) if i != col("character_pet", "id"))
    for r in rows["character_equipmentsets"]["rows"]:
        sets[r[col("character_equipmentsets", "setguid")]] = "set:" + str(r[col("character_equipmentsets", "setindex")])
    out = {}
    for table, data in rows.items():
        names = [c.split(" ")[0] for c in data["columns"]]
        canon = []
        for r in data["rows"]:
            r = list(r)
            for c, kind in spec.get(table, {}).items():
                i = names.index(c)
                v = r[i]
                if kind in ("Item", "ItemInSet"):
                    r[i] = items.get(v, v if v in (None, "0") else "missing-item")
                elif kind == "ItemRaw":
                    low = str(int(v) & 0xFFFFFFFF) if v not in (None, "0") else None
                    r[i] = items.get(low, v) if low else v
                elif kind == "Pet":
                    r[i] = pets.get(v, "missing-pet")
                elif kind == "EquipmentSet":
                    r[i] = sets.get(v, "missing-set")
            for c in ignore.get(table, []):
                if c in names:
                    r[names.index(c)] = "*"
            canon.append(tuple("" if v is None else str(v) for v in r))
        out[table] = sorted(canon)
    return out


def diff(a: dict, b: dict) -> dict:
    """{table: {"only_a": [...], "only_b": [...]}} for tables whose normalized rows differ (at most 5 rows each)."""
    out = {}
    for t in sorted(set(a) | set(b)):
        ra, rb = a.get(t, []), b.get(t, [])
        if ra != rb:
            sa, sb = list(ra), list(rb)
            for x in list(sa):
                if x in sb:
                    sb.remove(x)
                    sa.remove(x)
            out[t] = {"only_a": [list(x)[:12] for x in sa[:5]], "only_b": [list(x)[:12] for x in sb[:5]],
                      "count_a": len(ra), "count_b": len(rb)}
    return out


def rows_digest(guid: int) -> dict:
    """sha256 per save-set table of the character's rows (raw, ids included): for byte-identity checks."""
    out = {}
    for t, data in db_rows(guid).items():
        out[t] = hashlib.sha256(json.dumps(sorted(data["rows"], key=lambda r: [str(x) for x in r])).encode()).hexdigest()
    return out


# ----------------------------------------------------------------------------- shared-database safety and PLAYER

_SAFETY: dict = {}


def safety_dump() -> dict:
    """Once per harness process, before the first load: mysqldump of the characters database into a run-scoped folder
    <lane tmp>/saves-safety-<run>/ (lane A: env/tmp; never env/backups). Older saves-safety-* folders are removed, so only the latest
    copy is kept. Returns {"path", "bytes"}; raises SavesError if the dump fails (no load runs without it)."""
    if _SAFETY:
        return _SAFETY
    run = os.environ.get("HS_RUN_ID") or f"pid{os.getpid()}-{int(time.time())}"
    root = P.TMP                     # this lane's tmp (lane A: env/tmp)
    dest_dir = root / f"saves-safety-{re.sub(r'[^A-Za-z0-9_.-]', '_', run)}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "acore_characters.sql.gz"
    cmd = [str(P.MYSQLDUMP), f"--defaults-file={db.client_cnf()}", "--single-transaction", "--set-gtid-purged=OFF",
           "--hex-blob", "--default-character-set=utf8mb4", P.DB_NAMES["characters"]]
    with open(dest, "wb") as fh:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0 or not p.stdout:
            raise SavesError("safety copy of the characters database failed", stderr=p.stderr.decode(errors="replace")[-400:])
        fh.write(gzip.compress(p.stdout, 6))
    for old in root.glob("saves-safety-*"):
        if old != dest_dir:
            shutil.rmtree(old, ignore_errors=True)
    _SAFETY.update({"path": str(dest),
                    "bytes": dest.stat().st_size})
    return _SAFETY


def player_account() -> str | None:
    """The PLAYER account name (env/player.json, read-only; the password is never read into evidence)."""
    try:
        return str(json.loads(P.PLAYER_FILE.read_text())["account"]).upper()
    except (OSError, ValueError, KeyError):
        return None


def player_rows() -> dict:
    """{character guid: rows_digest} of the PLAYER account's characters (read-only)."""
    acct = player_account()
    if not acct:
        return {}
    aid = db.scalar(f"SELECT id FROM account WHERE username='{acct}'", "auth")
    if not aid:
        return {}
    guids = [int(r[0]) for r in db.query(f"SELECT guid FROM characters WHERE account={int(aid)}", "characters")]
    return {str(g): rows_digest(g) for g in guids}


_PLAYER_BASE: dict = {}


def player_baseline() -> dict:
    """The PLAYER characters' rows, recorded once per harness process by the first saves check that writes anything
    (saves.player_untouched compares them at the end of the run)."""
    if "rows" not in _PLAYER_BASE:
        _PLAYER_BASE["rows"] = player_rows()
        _PLAYER_BASE["t"] = time.time()
    return _PLAYER_BASE


def before_writes() -> dict:
    """Every saves check calls this before its first save or load: PLAYER baseline + the safety copy."""
    return {"player_baseline_taken": player_baseline()["t"], "safety_copy": safety_dump()}


# ----------------------------------------------------------------------------- the account and characters

def account_id() -> int | None:
    v = db.scalar(f"SELECT id FROM account WHERE username='{ident().user}'", "auth")
    return int(v) if v else None


def char_row(name: str) -> dict | None:
    rows = db.query(f"SELECT guid, account, level, online, map, zone, money, class FROM characters WHERE name='{name}'",
                    "characters")
    if not rows:
        return None
    g, a, lv, on, m, z, mo, c = rows[0]
    return {"guid": int(g), "account": int(a), "level": int(lv), "online": int(on), "map": int(m), "zone": int(z),
            "money": int(mo), "class": int(c)}


def ensure() -> dict:
    """The saves account (security GM_LEVEL) and its two characters, through the server."""
    ev = {}
    if account_id() is None:
        ev["create"] = gm(f"account create {ident().user} {ident().password}")
        _, ok, _ = F.poll(account_id, bool, timeout=15)
        if not ok:
            raise SavesError("saves account not in auth.account after `account create`", **ev)
    acct = account_id()
    if F.access_rows(acct) != [[str(GM_LEVEL), "-1"]]:
        ev["gmlevel"] = gm(f"account set gmlevel {ident().user} {GM_LEVEL} -1")
        _, ok, _ = F.poll(lambda: F.access_rows(acct), lambda v: v == [[str(GM_LEVEL), "-1"]], timeout=15)
        if not ok:
            raise SavesError("saves account security level not set", **ev)
    missing = [(n, r, c) for n, r, c in ((ident().char, RACE_DWARF, CLASS_HUNTER), (ident().alt, F.RACE_HUMAN, F.CLASS_WARRIOR))
               if not char_row(n)]
    if missing:
        _, ws, _ = probe.open_world(ident().user, ident().password)
        with ws:
            for n, r, c in missing:
                code = ws.char_create(n, r, c)
                if code != W.CHAR_CREATE_SUCCESS:
                    raise SavesError("CMSG_CHAR_CREATE rejected", name=n, result=f"0x{code:02X}")
        for n, _, _ in missing:
            _, ok, _ = F.poll(lambda: char_row(n), bool, timeout=15)
            if not ok:
                raise SavesError("character not in the database after CHAR_CREATE_SUCCESS", name=n)
        ev["created"] = [n for n, _, _ in missing]
    return ev | {"account": ident().user, "id": acct}


def wait_offline(name: str, timeout: float = 70) -> bool:
    _, ok, _ = F.poll(lambda: char_row(name)["online"], lambda v: v == 0, timeout=timeout)
    return ok


def store_dir(guid: int) -> Path:
    return P.SAVES / ident().user / str(int(guid))


def store_ids(guid: int) -> list[str]:
    d = store_dir(guid)
    return sorted(x.name for x in d.iterdir() if x.is_dir()) if d.is_dir() else []


def store_tmp(guid: int) -> list[str]:
    return [x for x in store_ids(guid) if x.startswith(".tmp-")]


def clear_store(guid: int) -> list[str]:
    """Remove the saves account's character folder (test data only: never another account's folder)."""
    d = store_dir(guid)
    if ident().user not in d.parts:
        raise SavesError("refusing to clear a folder outside the saves account", dir=str(d))
    gone = store_ids(guid)
    shutil.rmtree(d, ignore_errors=True)
    return gone


def manifest(guid: int, sid: str) -> dict:
    return json.loads((store_dir(guid) / sid / "manifest.json").read_text())


def server_list(guid: int) -> list[dict]:
    """`.moonglade save list <guid>`: [{id, kind, level, label}] newest first."""
    out = []
    for line in gm(f".moonglade save list {int(guid)}").splitlines():
        m = re.match(r"Moonglade save: (\d{8}-\d{6}-[a-z-]+(?:-\d+)?) (\S+) (\d+) (.*)$", line.strip())
        if m:
            out.append({"id": m.group(1), "kind": m.group(2), "level": int(m.group(3)), "label": m.group(4)})
    return out


def take(guid: int, label: str, timeout: float = 10) -> dict:
    """`.moonglade save take` and wait for the finished folder. Returns the new save's manifest."""
    before = set(store_ids(guid))
    out = gm(f'.moonglade save take {int(guid)} {label}')
    t = time.time()
    while time.time() - t < timeout:
        new = [x for x in store_ids(guid) if x not in before and not x.startswith(".tmp-")]
        if new:
            return manifest(guid, new[0]) | {"take_output": out}
        time.sleep(0.1)
    raise SavesError("no save folder appeared", take_output=out, before=sorted(before), after=store_ids(guid))


def load(guid: int, sid: str, timeout: float = 15) -> dict:
    """`.moonglade save load` + `.moonglade save status <job>` until it ends. {job, state, why, secs}."""
    t0 = time.time()
    out = gm(f".moonglade save load {int(guid)} {sid}")
    m = re.search(r"job (\d+)", out)
    if not m:
        raise SavesError("load not accepted", output=out)
    job = int(m.group(1))
    st = ""
    while time.time() - t0 < timeout:
        st = gm(f".moonglade save status {job}")
        sm = re.search(rf"job {job} (pending|done|refused|failed)(?:: (.*))?", st)
        if sm and sm.group(1) != "pending":
            return {"job": job, "state": sm.group(1), "why": (sm.group(2) or "").strip(), "secs": round(time.time() - t0, 2)}
        time.sleep(0.1)
    return {"job": job, "state": "timeout", "why": st, "secs": round(time.time() - t0, 2)}


# ----------------------------------------------------------------------------- an in-world session

class Session:
    """A saves-account character in the world through the probe (pumped). Context manager: on exit `.combatstop` and a
    logout (unless the character already left the world, e.g. by an in-game load)."""

    def __init__(self, name: str | None = None, tele: str | None = START_TELE):
        self.name, self.tele = name or ident().char, tele
        self.ws = None
        self.guid = None
        self.ev: dict = {}

    def __enter__(self):
        self.ev["ensure"] = ensure()
        if not wait_offline(self.name):
            raise SavesError("character still online before the session", row=char_row(self.name))
        if self.tele:
            self.ev["tele"] = gm(f"tele name {self.name} {F.tele_row(self.tele)['name']}")
        _, ws, _ = probe.open_world(ident().user, ident().password)
        self.ws = ws
        try:
            c = next((c for c in ws.char_enum() if c["name"].lower() == self.name.lower()), None)
            if not c:
                raise SavesError("character not in the character list", name=self.name)
            self.guid = c["guid"]
            self.ev["login"] = ws.player_login(self.guid)
            self.t_login = time.time()
            self.ev["settle"] = F.settle_login(ws, self.guid)
            ws.start_pump()
            self.ev["gm_off"] = ws.command(".gm off", settle=0.3, timeout=3)
            self.revive()
        except Exception:
            ws.close()
            raise
        return self

    def __exit__(self, *exc):
        if not self.ws:
            return
        try:
            if self.ws.player_guid and not self.ws.pump_error:
                self.ev["combatstop"] = self.ws.command(".combatstop", settle=0.3, timeout=3)
                self.ev["logout"] = self.ws.logout_pumped()
        except Exception as e:  # noqa: BLE001  (cleanup; the check's own result stands)
            self.ev["logout_error"] = f"{type(e).__name__}: {e}"
        finally:
            self.ws.close()

    def after_login(self, seconds: float = 6.0):
        """Wait until `seconds` after the login: the module doesn't count the map a character logs in to as an
        arrival for this long (MoongladeSaves.cpp OnPlayerMapChanged, 5 s)."""
        time.sleep(max(0.0, seconds - (time.time() - self.t_login)))

    def revive(self):
        pinfo = gm(f"pinfo {self.name}")
        alive = re.search(r"Alive \?: (\w+)", pinfo)
        if alive and alive.group(1) != "Yes":
            self.ev["revive"] = self.ws.command(".revive", settle=0.5, timeout=5)

    def cmd(self, text: str, settle: float = 0.5, timeout: float = 6) -> list[str]:
        return self.ws.command(text, settle=settle, timeout=timeout)

    def send(self, text: str):
        self.ws.chat(f"{PREFIX}\t{text}", W.CHAT_MSG_WHISPER, to=self.name, lang=LANG_ADDON)

    def replies(self, since: int) -> list[dict]:
        return [{"t": e["t"], "line": e["msg"][len(PREFIX) + 1:]} for e in self.ws.events_since(since, "chat")
                if e.get("msg", "").startswith(PREFIX + "\t")]

    def ask(self, text: str, end=("OK", "NO", "END"), timeout: float = 8) -> list[str]:
        since = self.ws.mark()
        self.send(text)
        t = time.time()
        while time.time() - t < timeout:
            ls = [r["line"] for r in self.replies(since)]
            if any(l.split("\t")[0] in end for l in ls):
                return ls
            if self.ws.pump_error:
                break
            time.sleep(0.05)
        return [r["line"] for r in self.replies(since)]

    def list(self) -> dict:
        """MGSAVE LIST -> {"gen", "rows": [{gen, index, kind, when, level, zone, label}], "lines"}."""
        lines = self.ask("LIST", end=("END",))
        rows = []
        gen = None
        for l in lines:
            f = l.split("\t")
            if f[0] == "E" and len(f) >= 8:
                rows.append({"gen": f[1], "index": int(f[2]), "kind": f[3], "when": f[4], "level": f[5], "zone": f[6],
                             "label": f[7]})
            elif f[0] == "END":
                gen = f[1]
        return {"gen": gen, "rows": rows, "lines": lines}

    def values(self, field: int, since: int = 0) -> int | None:
        """The last value the server sent for one of the character's own update fields since event `since`."""
        v = None
        for e in self.ws.events_since(since, "self_values"):
            if field in e["fields"]:
                v = e["fields"][field]
        return v


# ----------------------------------------------------------------------------- world-data rules (SQL / DBC)

TAME_RULE = ("a tameable beast for `.pet create` (cs_pet.cpp HandlePetCreateCommand: the selected creature's template "
             "needs a family; CreatureTemplate::IsTameable: type CREATURE_TYPE_BEAST (1), family != 0, type_flags & "
             "CREATURE_TYPE_FLAG_TAMEABLE (0x1), not exotic): the spawn on the start point's map nearest the start point "
             "with ct.rank 0")


def pick_tameable(tele: str = START_TELE) -> dict:
    t = F.tele_row(tele)
    rows = db.query(
        f"SELECT c.guid, c.id, ct.name, c.position_x, c.position_y, c.position_z FROM creature c JOIN creature_template ct "
        f"ON ct.entry = c.id WHERE c.map = {t['map']} AND ct.type = {CREATURE_TYPE_BEAST} AND ct.family <> 0 "
        f"AND (ct.type_flags & {CREATURE_TYPE_FLAG_TAMEABLE}) <> 0 AND ct.rank = 0 "
        f"ORDER BY POW(c.position_x-({t['x']}),2)+POW(c.position_y-({t['y']}),2), c.guid LIMIT 5", "world")
    if not rows:
        raise SavesError("no creature matches the rule", rule=TAME_RULE)
    return [{"spawn_id": int(g), "entry": int(e), "name": n, "pos": {"x": float(x), "y": float(y), "z": float(z)},
             "rule": TAME_RULE} for g, e, n, x, y, z in rows]


BANKER_RULE = ("the banker (creature_template.npcflag & UNIT_NPC_FLAG_BANKER 0x20000, UnitDefines.h) on map 0 nearest "
               "the Stormwind game_tele row")


def pick_banker() -> dict:
    t = F.tele_row("Stormwind")
    rows = db.query(
        f"SELECT c.guid, c.id, ct.name, c.position_x, c.position_y, c.position_z FROM creature c JOIN creature_template ct "
        f"ON ct.entry = c.id WHERE c.map = {t['map']} AND (ct.npcflag & {UNIT_NPC_FLAG_BANKER}) <> 0 "
        f"ORDER BY POW(c.position_x-({t['x']}),2)+POW(c.position_y-({t['y']}),2), c.guid LIMIT 1", "world")
    if not rows:
        raise SavesError("no banker matches the rule", rule=BANKER_RULE)
    g, e, n, x, y, z = rows[0]
    return {"spawn_id": int(g), "entry": int(e), "name": n, "pos": {"x": float(x), "y": float(y), "z": float(z)},
            "rule": BANKER_RULE}


TALENT_RULE = ("a hunter talent: the Talent.dbc row (TalentEntry: 0 ID, 1 TalentTab, 2 Row, 3 Col, 4-8 RankID) whose "
               "TalentTab.dbc tab (TalentTabEntry: 0 ID, 20 ClassMask) has the hunter bit (1 << (3 - 1)), in row 0, "
               "lowest ID; its rank-1 spell")


def pick_talent() -> dict:
    tabs, _ = dbc.read_dbc("TalentTab.dbc")
    hunter_tabs = {r[0] for r in tabs if r[20] & (1 << (CLASS_HUNTER - 1))}
    talents, _ = dbc.read_dbc("Talent.dbc")
    rows = sorted(r for r in talents if r[1] in hunter_tabs and r[2] == 0 and r[4])
    if not rows:
        raise SavesError("no talent matches the rule", rule=TALENT_RULE)
    r = rows[0]
    return {"talent": r[0], "tab": r[1], "spell": r[4], "rule": TALENT_RULE}


SKILL_RULE = ("a profession skill for `.setskill`: the SkillLine.dbc row (SkillLineEntry: 0 ID, 1 categoryId, 3 name "
              "string offset) in category 9 (secondary skills: SkillLineCategory.dbc) named \"First Aid\"")


def pick_skill() -> dict:
    recs, strings = dbc.read_dbc("SkillLine.dbc")
    for r in recs:
        if r[1] == 9 and dbc.dbc_string(strings, r[3]) == "First Aid":
            return {"skill": r[0], "rule": SKILL_RULE}
    raise SavesError("no skill matches the rule", rule=SKILL_RULE)


QUEST_RULE = ("an active quest for `.quest add`: fixtures.QUEST_ID (validated in quest_template; a kill/collect quest, "
              "so it stays incomplete)")
ITEM_RULE = "items for bags and bank: fixtures.ITEM_ENTRY (validated in item_template)"


def item_guid_full(low: int) -> int:
    return (0x4000 << 48) | int(low)            # ObjectGuid.h HighGuid::Item = 0x4000


def pack_guid(g: int) -> bytes:
    return W.pack_guid(g)


def destroy_item(ws, bag: int, slot: int, count: int = 0):
    ws.send(CMSG_DESTROYITEM, struct.pack("<BBI", bag, slot, count))


def remove_quest_slot(ws, slot: int):
    ws.send(CMSG_QUESTLOG_REMOVE_QUEST, struct.pack("<B", slot))


def save_equipment_set(ws, index: int, name: str, icon: str, items: list[int]):
    """items = 19 item guids (low counter, 0 = empty); a new set (set guid 0)."""
    body = pack_guid(0) + struct.pack("<I", index) + name.encode() + b"\x00" + icon.encode() + b"\x00"
    for low in (items + [0] * EQUIPMENT_SLOT_END)[:EQUIPMENT_SLOT_END]:
        body += pack_guid(item_guid_full(low) if low else 0)
    ws.send(CMSG_EQUIPMENT_SET_SAVE, body)


# ----------------------------------------------------------------------------- the round-trip character

RICH_LEVEL = 20                  # >= 10 for a talent point (Player::InitTalentForLevel) and for the pet
RICH_MONEY = 123456              # copper; any distinct amount
SET_NAME, SET_ICON = "Hs set", "INV_Misc_QuestionMark"
# `.modify reputation <faction> <value>` sets the ABSOLUTE reputation (cs_modify.cpp HandleModifyRepCommand ->
# ReputationMgr::SetOneFactionReputation(..., incremental = false)); character_reputation.standing stores it relative to
# the race/class base (ReputationMgr::GetBaseReputation, Faction.dbc BaseRepValue). So the checks set fixed absolute
# values, never "the stored standing + delta" (which drifted to the floor, -42000, run after run).
REP_GIVEN = 1000                  # Neutral (0..2999, ReputationMgr::PointsInRank)
REP_CHANGED = 2500                # still Neutral, a different value


def _self_cmd(s: Session, text: str) -> list[str]:
    s.ws.set_selection(s.guid)
    return s.cmd(text)


def inventory(guid: int) -> list[dict]:
    rows = db.query(f"SELECT ci.bag, ci.slot, ci.item, ii.itemEntry FROM character_inventory ci JOIN item_instance ii "
                    f"ON ii.guid = ci.item WHERE ci.guid = {int(guid)} ORDER BY ci.bag, ci.slot", "characters")
    return [{"bag": int(b), "slot": int(sl), "item": int(i), "entry": int(e)} for b, sl, i, e in rows]


def faction_rule(guid: int) -> dict:
    """A reputation to change: the character's own character_reputation row with the lowest faction id among those
    with FACTION_FLAG_VISIBLE (0x1, ReputationMgr.h) set."""
    rows = db.query(f"SELECT faction, standing FROM character_reputation WHERE guid = {int(guid)} AND (flags & 1) = 1 "
                    "ORDER BY faction LIMIT 1", "characters")
    if not rows:
        raise SavesError("no visible reputation row")
    return {"faction": int(rows[0][0]), "standing": int(rows[0][1]),
            "rule": "the character's visible reputation (flags & FACTION_FLAG_VISIBLE 0x1) with the lowest faction id"}


def rep_absolute(guid: int) -> int | None:
    """The character's absolute reputation with the faction_rule faction: the stored standing plus the base for its
    race and class (ReputationMgr::GetBaseReputation over Faction.dbc: FactionEntry 0 ID, 2-5 BaseRepRaceMask,
    6-9 BaseRepClassMask, 10-13 BaseRepValue)."""
    fac = faction_rule(guid)
    row = db.query(f"SELECT race, class FROM characters WHERE guid = {int(guid)}", "characters")[0]
    race_mask, class_mask = 1 << (int(row[0]) - 1), 1 << (int(row[1]) - 1)
    recs, _ = dbc.read_dbc("Faction.dbc")
    f = next((r for r in recs if r[0] == fac["faction"]), None)
    if f is None:
        return None
    base = 0
    for i in range(4):
        rm, cm, val = f[2 + i], f[6 + i], f[10 + i]
        if ((rm & race_mask) or (rm == 0 and cm != 0)) and ((cm & class_mask) or cm == 0):
            base = val - (1 << 32) if val >= 1 << 31 else val
            break
    return base + fac["standing"]


def prepare_rich() -> dict:
    """GIVEN of saves.roundtrip, through the server and the probe: the hunter at RICH_LEVEL with a pet, RICH_MONEY,
    its starting gear equipped, fixtures.ITEM_ENTRY in its bags and in its bank, an equipment set, the active
    fixtures.QUEST_ID, a talent, First Aid and a changed reputation; logged out (saved). Returns what was set up."""
    ev = {"ensure": ensure()}
    if not wait_offline(ident().char):
        raise SavesError("character still online")
    ev["level"] = gm(f"character level {ident().char} {RICH_LEVEL}")
    talent, skill = pick_talent(), pick_skill()
    with Session(ident().char) as s:
        g = s.guid
        ev["guid"] = g
        if not db.query(f"SELECT id FROM character_pet WHERE owner = {g}", "characters"):
            for beast in pick_tameable():
                ev["beast"] = beast
                p = beast["pos"]
                ev["go_beast"] = s.cmd(f".go xyz {p['x']} {p['y']} {p['z']} 0")
                time.sleep(1.0)
                info = s.cmd(f".npc info {beast['spawn_id']}")
                m = re.search(r"current GUID: GUID Full: 0x([0-9a-fA-F]+)", "\n".join(info))
                if not m:
                    continue
                # test-pyramid (shared server): an earlier test may have killed this spawn; a dead one can't be
                # tamed, so the rule's next candidate is tried (the GIVEN below still requires the pet)
                hp = re.search(r"\(current\): (\d+)\.", "\n".join(info))
                if hp and int(hp.group(1)) == 0:
                    ev.setdefault("beasts_dead", []).append(beast["spawn_id"])
                    continue
                s.ws.set_selection(int(m.group(1), 16))
                ev["pet_create"] = s.cmd(".pet create")
                if db.query(f"SELECT id FROM character_pet WHERE owner = {g}", "characters") or \
                        not any("select" in l.lower() for l in ev["pet_create"]):
                    break
                ev.setdefault("beasts_refused", []).append({"spawn_id": beast["spawn_id"], "reply": ev["pet_create"]})
        ev["money"] = _self_cmd(s, f".modify money {RICH_MONEY}")
        ev["additem"] = _self_cmd(s, f".additem {F.ITEM_ENTRY} 3")
        ev["quest"] = _self_cmd(s, f".quest add {F.QUEST_ID}")
        s.ws.send(CMSG_LEARN_TALENT, struct.pack("<II", talent["talent"], 0))
        ev["talent"] = talent
        ev["skill"] = skill | {"set": _self_cmd(s, f".setskill {skill['skill']} 50 75")}
        ev["save1"] = s.cmd(".save")
        time.sleep(1.0)
        fac = faction_rule(g)
        ev["reputation"] = fac | {"modify": _self_cmd(s, f".modify reputation {fac['faction']} {REP_GIVEN}")}
        # an equipment set of the equipped items (CMSG_EQUIPMENT_SET_SAVE)
        inv = inventory(g)
        equipped = [0] * EQUIPMENT_SLOT_END
        for it in inv:
            if it["bag"] == 0 and it["slot"] < EQUIPMENT_SLOT_END:
                equipped[it["slot"]] = it["item"]
        save_equipment_set(s.ws, 0, SET_NAME, SET_ICON, equipped)
        # one of the bag items into the bank: at the banker (CMSG_BANKER_ACTIVATE, then CMSG_AUTOBANK_ITEM)
        banker = pick_banker()
        ev["banker"] = banker
        bp = banker["pos"]
        ev["go_banker"] = s.cmd(f".go xyz {bp['x']} {bp['y']} {bp['z']} 0")
        time.sleep(1.0)
        info = s.cmd(f".npc info {banker['spawn_id']}")
        m = re.search(r"current GUID: GUID Full: 0x([0-9a-fA-F]+)", "\n".join(info))
        backpack = [it for it in inventory(g) if it["bag"] == 0 and INVENTORY_SLOT_ITEM_START <= it["slot"] < 39
                    and it["entry"] == F.ITEM_ENTRY]
        if m and backpack:
            s.ws.send(CMSG_BANKER_ACTIVATE, struct.pack("<Q", int(m.group(1), 16)))
            time.sleep(0.5)
            s.ws.send(CMSG_AUTOBANK_ITEM, struct.pack("<BB", INVENTORY_SLOT_BAG_0, backpack[0]["slot"]))
            time.sleep(0.5)
        ev["additem_bags"] = _self_cmd(s, f".additem {F.ITEM_ENTRY} 2")      # (the first stack went to the bank)
        ev["save2"] = s.cmd(".save")
        ev["tele_back"] = gm(f"tele name {ident().char} {F.tele_row(START_TELE)['name']}")
        time.sleep(1.0)
    ev["session"] = {k: v for k, v in s.ev.items() if k in ("login", "logout", "logout_error")}
    if not wait_offline(ident().char):
        raise SavesError("character didn't log out", **ev)
    ev["given"] = given_rich(ev["guid"])
    return ev


def given_rich(guid: int) -> dict:
    """What the round trip's GIVEN needs, read from the database: each must be true."""
    q = lambda sql: db.scalar(sql, "characters")                     # noqa: E731
    inv = inventory(guid)
    g = {
        "level": int(q(f"SELECT level FROM characters WHERE guid={guid}")) >= 10,
        "pet": int(q(f"SELECT COUNT(*) FROM character_pet WHERE owner={guid}")) >= 1,
        "money": int(q(f"SELECT money FROM characters WHERE guid={guid}")) > 0,
        "equipped": any(i["bag"] == 0 and i["slot"] < EQUIPMENT_SLOT_END for i in inv),
        "in_bags": any(i["bag"] == 0 and INVENTORY_SLOT_ITEM_START <= i["slot"] < 39 for i in inv),
        "in_bank": any(i["bag"] == 0 and 39 <= i["slot"] < 67 for i in inv),     # Player.h BANK_SLOT_ITEM_START..END
        "equipment_set": int(q(f"SELECT COUNT(*) FROM character_equipmentsets WHERE guid={guid}")) >= 1,
        "active_quest": int(q(f"SELECT COUNT(*) FROM character_queststatus WHERE guid={guid} AND quest={F.QUEST_ID}")) == 1,
        "talent": int(q(f"SELECT COUNT(*) FROM character_talent WHERE guid={guid}")) >= 1,
        "skill": int(q(f"SELECT COUNT(*) FROM character_skills WHERE guid={guid} AND skill={pick_skill()['skill']}")) == 1,
        "reputation": rep_absolute(guid) == REP_GIVEN,
    }
    return g


# ----------------------------------------------------------------------------- the server's view, combat, mail, auctions

CMSG_ATTACKSWING = 0x141            # CombatHandler.cpp HandleAttackSwingOpcode: u64 target
CMSG_SEND_MAIL = 0x238              # MailHandler.cpp HandleSendMail: u64 mailbox, receiver, subject, body, u32 stationery,
                                    # u32 0, u8 item count, per item (u8 slot, u64 item guid), u32 money, u32 COD, u64 0, u8 0
CMSG_AUCTION_SELL_ITEM = 0x256      # AuctionHouseHandler.cpp HandleAuctionSellItem: u64 auctioneer, u32 item count,
                                    # per item (u64 guid, u32 count), u32 bid, u32 buyout, u32 minutes
CMSG_ADD_FRIEND = 0x069             # SocialHandler.cpp HandleAddFriendOpcode: name, note
AUCTION_MINUTES = 12 * 60           # AuctionHouseMgr.h MIN_AUCTION_TIME (12 h); the handler accepts 1x/2x/4x of it
UNIT_NPC_FLAG_AUCTIONEER = 0x200000  # UnitDefines.h NPCFlags


def who(guid: int) -> dict:
    """`.moonglade save who`: the server's in-memory level, money, position, combat, dead, bot, resting, flying and
    quests (online only)."""
    out = gm(f".moonglade save who {int(guid)}")
    m = re.search(r"level (\d+) money (\d+) map (\d+) x (\S+) y (\S+) z (\S+) combat (\d) dead (\d) bot (\d) "
                  r"resting (\d) flying (\d) quests (\S+)", out)
    if not m:
        return {"online": False, "raw": out}
    q = {} if m.group(12) == "-" else {int(a): int(b) for a, b in (x.split(":") for x in m.group(12).split(","))}
    return {"online": True, "level": int(m.group(1)), "money": int(m.group(2)), "map": int(m.group(3)),
            "x": float(m.group(4)), "y": float(m.group(5)), "z": float(m.group(6)), "combat": m.group(7) == "1",
            "dead": m.group(8) == "1", "bot": m.group(9) == "1", "resting": m.group(10) == "1",
            "flying": m.group(11) == "1", "quests": q}


def npc_guid(s: Session, spawn_id: int) -> int | None:
    info = s.cmd(f".npc info {int(spawn_id)}")
    m = re.search(r"current GUID: GUID Full: 0x([0-9a-fA-F]+)", "\n".join(info))
    return int(m.group(1), 16) if m else None


def engage(s: Session, level: int = 8, timeout: float = 25) -> dict:
    """GIVEN "in combat": the character goes (`.go xyz`) to the nearest live, unhurt hostile creature of
    companion.CREATURE_RULE for its level, auto-attacks it (CMSG_ATTACKSWING), and the server reports it in combat
    (`.moonglade save who`)."""
    from . import companion as C
    tried = []
    t0 = time.time()
    for cr in C.creature_candidates(level, START_TELE, n=12):
        if time.time() - t0 > timeout:
            break
        info = C.parse_npc_info(s.cmd(f".npc info {cr['spawn_id']}"))
        tried.append({"spawn": cr["spawn_id"], "health": info["health"], "max": info["max_health"], "combat": info["in_combat"]})
        if not info["guid"] or not info["pos"] or info["health"] != info["max_health"] or info["in_combat"]:
            continue
        p = info["pos"]
        s.cmd(f".go xyz {p['x']:.2f} {p['y']:.2f} {p['z']:.2f}", settle=0.3)
        s.ws.set_selection(info["guid"])
        s.ws.send(CMSG_ATTACKSWING, struct.pack("<Q", info["guid"]))
        for _ in range(16):
            w = who(s.guid)
            if w.get("combat"):
                return {"creature": cr | {"guid": info["guid"]}, "rule": C.CREATURE_RULE, "who": w, "tried": tried}
            time.sleep(0.25)
    raise SavesError("precondition: the character never got into combat", tried=tried)


def pick_auctioneer(tele: str = "Stormwind") -> dict:
    t = F.tele_row(tele)
    rows = db.query(
        f"SELECT c.guid, c.id, c.position_x, c.position_y, c.position_z FROM creature c JOIN creature_template ct "
        f"ON ct.entry = c.id WHERE c.map = {t['map']} AND (ct.npcflag & {UNIT_NPC_FLAG_AUCTIONEER}) <> 0 "
        f"ORDER BY POW(c.position_x-({t['x']}),2)+POW(c.position_y-({t['y']}),2), c.guid LIMIT 1", "world")
    if not rows:
        raise SavesError("no auctioneer on the map")
    g, e, x, y, z = rows[0]
    return {"spawn_id": int(g), "entry": int(e), "pos": {"x": float(x), "y": float(y), "z": float(z)},
            "rule": "the auctioneer (npcflag & UNIT_NPC_FLAG_AUCTIONEER 0x200000) on map 0 nearest the Stormwind game_tele row"}


def go(s: Session, pos: dict):
    s.cmd(f".go xyz {pos['x']:.2f} {pos['y']:.2f} {pos['z']:.2f}", settle=0.5)


def mail_item(s: Session, to: str, item_low: int) -> dict:
    """Mail one of the character's own items to `to` (CMSG_SEND_MAIL). The mailbox is the character itself, as the GM
    `.mailbox` command opens it (MailHandler.cpp CanOpenMailBox: the player's own guid with RBAC_PERM_COMMAND_MAILBOX,
    which the saves account's security level has); a world mailbox's in-world guid isn't its database guid."""
    mb = {"guid": s.guid, "how": "the character's own mailbox (GM .mailbox)"}
    body = (struct.pack("<Q", mb["guid"]) + to.encode() + b"\x00" + b"hs saves\x00" + b"hs saves item\x00"
            + struct.pack("<II", 41, 0) + struct.pack("<B", 1) + struct.pack("<BQ", 0, item_guid_full(item_low))
            + struct.pack("<IIQB", 0, 0, 0, 0))
    s.ws.send(CMSG_SEND_MAIL, body)
    time.sleep(1.0)
    return {"mailbox": mb}


def sell_auction(s: Session, item_low: int, bid: int = 100, buyout: int = 0) -> dict:
    au = pick_auctioneer()
    go(s, au["pos"])
    g = npc_guid(s, au["spawn_id"])
    if not g:
        raise SavesError("auctioneer not found in the world", auctioneer=au)
    s.ws.send(CMSG_AUCTION_SELL_ITEM, struct.pack("<QI", g, 1) + struct.pack("<QI", item_guid_full(item_low), 1)
              + struct.pack("<III", bid, buyout, AUCTION_MINUTES))
    time.sleep(1.0)
    return {"auctioneer": au}


def add_friend(s: Session, name: str):
    s.ws.send(CMSG_ADD_FRIEND, name.encode() + b"\x00" + b"\x00")
    time.sleep(0.8)
