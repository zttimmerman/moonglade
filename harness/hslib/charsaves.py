"""The launcher's side of per-character saves (openspec/changes/front-menu-saves, design.md 2, 5 and 6).

mod-moonglade writes every save (MoongladeSaves.cpp); the launcher only reads the store and asks the server to load:
- the store: `<lane env>/saves/<ACCOUNT>/<character id>/<save id>/` with `manifest.json` and `character.json.gz`
  (P.SAVES; the player's is P.PLAYER_SAVES = env/saves). Folders named `.tmp-*` are unfinished saves: never listed
  (the module removes them when it starts).
- the character screen's list: `HS_SAVES` (Lua), rewritten in place in the play client's fixed-size slot
  (frontmenu.rewrite_saves) whenever the store changes; the glue screens read it each time they load (game start,
  back from the world: design.md "Prototype results", Q1). It names characters (by their current name), and per
  save an index, a label, a kind and a time. Never a save id, a path, the nonce or a password. The generation `gen`
  (one hex digit) changes with every new list; the file ends with `HS_SAVES_END = <gen>`, so the glue can tell a
  torn read (a rewrite racing a glue load) from a whole one.
- a load requested by the glue: `L<gen><nn>` over the control channel (savesctl.py). Saves.map((gen, nn)) gives the
  character and save id (only for the current generation; an older one is stale), then `.moonglade save load <guid>
  <id>` over SOAP and `.moonglade save status <job>` until it's done (at most LOAD_POLL_S).
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
from pathlib import Path

from . import paths as P

ID_RE = re.compile(r"^\d{8}-\d{6}-(manual|quick|auto|before-load)(-\d+)?$")
KIND_TEXT = {"manual": "Saved", "quick": "Quicksave", "auto": "Autosave", "before-load": "Before loading"}
MAX_PER_CHAR = 20            # the character screen lists at most this many saves per character (newest first)
MAX_INDEX = 99               # the token's index has two digits
LOAD_POLL_S = 10.0           # design.md 5: the launcher polls the load at most this long
POLL_S = 0.25                # design.md 6: the store is checked this often while the game runs (a save made right
                             # before leaving the world must be in the slot before the glue screens load again)


class SavesError(RuntimeError):
    pass


def store(root: Path | None = None) -> Path:
    return Path(root) if root else P.SAVES


def _account_dir(account: str, root: Path | None = None) -> Path:
    name = account.upper()
    if not re.fullmatch(r"[A-Z0-9]{1,32}", name):
        raise SavesError(f"bad account name {account!r}")
    return store(root) / name


def read_manifest(d: Path) -> dict | None:
    """A finished save's manifest, or None (unfinished, unreadable, or not ours)."""
    if d.name.startswith(".tmp-") or not ID_RE.match(d.name):
        return None
    try:
        m = json.loads((d / "manifest.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or m.get("format") != 1 or m.get("id") != d.name:
        return None
    return m


def saves_of(account: str, guid: int, root: Path | None = None) -> list[dict]:
    """One character's saves, newest first (the module's order: created, then id)."""
    d = _account_dir(account, root) / str(int(guid))
    out = [m for m in (read_manifest(x) for x in sorted(d.iterdir()) if x.is_dir()) if m] if d.is_dir() else []
    return sorted(out, key=lambda m: (int(m.get("created") or 0), m["id"]), reverse=True)


TMP_STALE_S = 600            # a .tmp-* this old is a save a crash interrupted (the module writes one in milliseconds)


def clean_tmp(accounts: list[str], root: Path | None = None, older_than_s: float = TMP_STALE_S) -> list[str]:
    """Remove unfinished saves (`.tmp-*`) older than older_than_s under the accounts' folders (the module also
    removes every one when it starts). Returns what was removed (relative to the store)."""
    gone, now = [], time.time()
    for a in accounts:
        d = _account_dir(a, root)
        for t in (sorted(d.glob("*/.tmp-*")) if d.is_dir() else []):
            if t.is_dir() and now - t.stat().st_mtime > older_than_s:
                shutil.rmtree(t, ignore_errors=True)
                gone.append(str(t.relative_to(store(root))))
    return gone


def characters(account: str) -> list[dict]:
    """The account's characters (read-only): [{guid, name, level, class}], by name."""
    from . import db
    name = account.upper()
    if not re.fullmatch(r"[A-Z0-9]{1,32}", name):
        raise SavesError(f"bad account name {account!r}")
    aid = db.scalar(f"SELECT id FROM account WHERE username='{name}'", "auth")
    if not aid:
        return []
    rows = db.query(f"SELECT guid, name, level, class FROM characters WHERE account={int(aid)} AND deleteDate IS NULL "
                    "ORDER BY name", "characters")
    return [{"guid": int(g), "name": n, "level": int(lv), "class": int(c)} for g, n, lv, c in rows]


def when_text(m: dict) -> str:
    t = int(m.get("created") or 0)
    return time.strftime("%a %d %b %H:%M", time.localtime(t)) if t else ""


def build(accounts: list[str], chars_of=characters, root: Path | None = None) -> tuple[dict, dict]:
    """(HS_SAVES content without the generation, index map {nn: (account, guid, save id)}). Characters without
    saves are listed too (empty), so the glue can say "No saves yet"."""
    out, index, nn = [], {}, 0
    for acct in accounts:
        for ch in chars_of(acct):
            entries = []
            for m in saves_of(acct, ch["guid"], root)[:MAX_PER_CHAR]:
                if nn >= MAX_INDEX:
                    break
                nn += 1
                index[nn] = (acct.upper(), ch["guid"], m["id"])
                entries.append({"idx": nn, "label": str(m.get("label") or ""), "kind": KIND_TEXT.get(m.get("kind"), ""),
                                "when": when_text(m), "level": int(m.get("level") or 0)})
            out.append({"name": ch["name"], "level": ch["level"], "class": ch["class"], "saves": entries})
    return {"chars": out}, index


def render(content: dict, gen: int) -> bytes:
    """HS_SAVES as Lua (frontmenu._lua quoting), ending with the generation marker."""
    from .frontmenu import _lua
    g = f"{gen:X}"
    chars = "{ " + ", ".join(_lua(c) for c in content["chars"]) + " }"
    return ("-- Generated by the Moonglade launcher (harness/hslib/charsaves.py): the character screen's save list.\r\n"
            f'HS_SAVES = {{ gen = "{g}", chars = {chars} }};\r\nHS_SAVES_END = "{g}";\r\n').encode("ascii")


class Saves:
    """The live list for one game session: keeps HS_SAVES current in the play client and maps a glue request
    (generation, index) to the save it names. Thread-safe (the listener thread asks while the poll thread writes)."""

    def __init__(self, client_dir: Path | None, accounts: list[str], root: Path | None = None, chars_of=characters,
                 write=None):
        self.client_dir = Path(client_dir) if client_dir else None
        self.accounts = [a.upper() for a in accounts]
        self.root = root
        self.chars_of = chars_of
        self.write = write                     # test hook: fn(lua bytes); default frontmenu.rewrite_saves
        self.gen = 0
        self.content: dict | None = None
        self.index: dict = {}
        self.sig = None
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def signature(self):
        """What changes when a save is added, finished or removed: the save folders' names and mtimes."""
        sig = []
        for a in self.accounts:
            d = _account_dir(a, self.root)
            if not d.is_dir():
                continue
            for c in sorted(d.iterdir()):
                if c.is_dir():
                    sig.append((a, c.name, c.stat().st_mtime_ns,
                                tuple(sorted(x.name for x in c.iterdir() if not x.name.startswith(".tmp-")))))
        return tuple(sig)

    def refresh(self, force: bool = False) -> bool:
        """Rebuild and rewrite the list if the store changed (or `force`). Returns whether it was rewritten."""
        sig = self.signature()
        with self._lock:
            if sig == self.sig and not force:
                return False
            content, index = build(self.accounts, self.chars_of, self.root)
            if content == self.content and not force:
                self.sig = sig
                return False
            self.gen = (self.gen + 1) % 16
            lua = render(content, self.gen)
            if self.write:
                self.write(lua)
            elif self.client_dir:
                from .frontmenu import rewrite_saves
                rewrite_saves(self.client_dir, lua)
            self.content, self.index, self.sig = content, index, sig
            self.events.append({"t": round(time.time(), 3), "event": "list", "gen": f"{self.gen:X}",
                                "saves": len(index)})
            del self.events[:-50]
            return True

    def lookup(self, arg: str) -> tuple[str, int, str] | None:
        """`<gen hex><nn>` -> (account, character guid, save id) for the current list only; None if stale or unknown."""
        m = re.fullmatch(r"([0-9A-F])(\d{2})", arg or "")
        if not m:
            return None
        with self._lock:
            if self.content is None or int(m.group(1), 16) != self.gen:
                return None
            return self.index.get(int(m.group(2)))

    # -- the poll thread (while the game runs)
    def start(self) -> "Saves":
        removed = clean_tmp(self.accounts, self.root)
        if removed:
            self.events.append({"t": round(time.time(), 3), "event": "tmp_removed", "count": len(removed)})
        self.refresh(force=True)
        self._thread = threading.Thread(target=self._loop, name="charsaves", daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(POLL_S):
            try:
                self.refresh()
            except Exception as e:  # noqa: BLE001  (a failed refresh keeps the old list; logged, never fatal)
                self.events.append({"t": round(time.time(), 3), "event": "refresh_error", "why": type(e).__name__})

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        return {"gen": f"{self.gen:X}", "saves": len(self.index), "events": self.events[-20:]}


def load_via_soap(guid: int, save_id: str, gm=None, poll_s: float = LOAD_POLL_S) -> tuple[bool, str]:
    """Ask the server to load a save (`.moonglade save load`), then poll `.moonglade save status <job>` until it is
    done, refused or failed, at most poll_s. (ok, reason); the reason is plain text without the save id."""
    if not ID_RE.match(save_id or ""):
        return False, "bad save"
    if gm is None:
        from .soap import gm as soap_gm
        gm = lambda c: soap_gm(c).output                     # noqa: E731
    out = gm(f".moonglade save load {int(guid)} {save_id}")
    m = re.search(r"Moonglade save: job (\d+)", out or "")
    if not m:
        return False, "the server didn't take the request"
    job = int(m.group(1))
    t = time.time()
    while time.time() - t < poll_s:
        st = gm(f".moonglade save status {job}") or ""
        sm = re.search(rf"job {job} (pending|done|refused|failed)(?:: (.*))?", st)
        if sm and sm.group(1) == "done":
            return True, "done"
        if sm and sm.group(1) in ("refused", "failed"):
            return False, (sm.group(2) or sm.group(1)).strip()
        time.sleep(0.2)
    return False, "the load didn't finish in time"


def delete(account: str, guid: int, save_id: str, root: Path | None = None) -> bool:
    """Remove one save folder of the account's character (only a finished save, never a path outside the store)."""
    if not ID_RE.match(save_id or ""):
        raise SavesError("bad save id")
    d = _account_dir(account, root) / str(int(guid)) / save_id
    if not (d / "manifest.json").is_file():
        return False
    shutil.rmtree(d)
    return True
