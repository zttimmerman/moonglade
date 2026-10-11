"""Shared-server fixture, PROTOTYPE (openspec/changes/test-pyramid design 6; task 1.2).

One running stack (the caller brings it up once). Each test runs under its own saves identity (a fresh account and
two fresh characters, created through the server like the saves account), then a reset round compares the shared
state with the state at the start: pause state, the saves status line, the running profile, and who is online
(minus the test's own characters, which must be offline again). A difference fails the test that left it
("leaked state"). Character-scoped tests may run two at a time (`workers=2`).

Names: accounts HSI<base36>, characters "Hs" + letters (character names allow letters only: ObjectMgr::CheckPlayerName,
isValidString(..., numericOrSpace=false), and no letter three times in a row; ObjectMgr.cpp at core 7f12e89).
Cleanup at the end: `.character erase` and `.account delete` over SOAP for the identities this run created, and
their folders in this lane's save store.
"""
from __future__ import annotations

import itertools
import re
import shutil
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db, paths as P, saves as S, soap, stack
from . import pause as Z
from .registry import CheckFail, load_all

_LOCK = threading.Lock()
_COUNTER = itertools.count()
LETTERS = "abcdefghijklmnopqrstuvwxyz"


def _letters(n: int) -> str:
    out = ""
    while True:
        n, r = divmod(n, 26)
        out = LETTERS[r] + out
        if n == 0:
            return out


def _b36(n: int) -> str:
    d = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = d[r] + out
        if n == 0:
            return out


def _triple(name: str) -> bool:
    """Three identical letters in a row, any case (AzerothCore refuses: CHAR_NAME_THREE_CONSECUTIVE)."""
    n = name.lower()
    return any(n[i] == n[i - 1] == n[i - 2] for i in range(2, len(n)))


def valid_char_name(name: str) -> bool:
    """AzerothCore's character-name rules the fixture relies on (ObjectMgr::CheckPlayerName at core 7f12e89):
    letters only, 2..12 characters, no letter three times in a row."""
    return 2 <= len(name) <= 12 and name.isascii() and name.isalpha() and not _triple(name)


def identity_names(k: int) -> tuple[str, str] | None:
    """The two character names for counter value k, or None when either full name would break a name rule (the
    rule applies to the whole name, prefix and suffix included: stem "eftgxx" + "x" -> "Hseftgxxx")."""
    stem = _letters(k)
    char, alt = ("Hs" + stem + "x").capitalize(), ("Hs" + stem + "z").capitalize()
    if not (valid_char_name(char) and valid_char_name(alt)) or char == alt:
        return None
    return char, alt


def new_identity(run_tag: int) -> S.Identity:
    with _LOCK:
        while True:
            k = run_tag * 1000 + next(_COUNTER)
            names = identity_names(k)
            if names is not None:
                break
    acct = f"HSI{_b36(k)}".upper()[:16]
    return S.Identity(acct, "hsi-" + _b36(k), *names)


def shared_state(own: set[str] = frozenset()) -> dict:
    """One reset round: what a test must leave as it found it (harness-control "Reset between shared-server tests"):
    pause state and switch, saves enabled and the autosave interval, the running config profile, and who is online
    (minus the fixture's own characters). Fail-closed: an error is a difference."""
    t0 = time.perf_counter()
    st = {}
    try:
        p = Z.status()
        st["pause"] = (p["state"], p["enabled"]) if p["ok"] else ("error", p["raw"])
        r = soap.gm("moonglade save status", timeout=10)
        out = r.output or ""
        m = re.search(r"auto interval: (\d+)", out)
        st["saves"] = {"enabled": out.startswith("Moonglade saves: enabled"),
                       "interval_s": int(m.group(1)) if m else None} if r.ok else "error"
        rp = stack.running_profile()
        st["profile"] = (rp or {}).get("profile")
        # AI players (random bots and the companion pool) come and go on their own: only other accounts count
        bots = P.DB_NAMES["playerbots"]
        rows = db.query("SELECT name FROM characters WHERE online = 1 AND account NOT IN "
                        f"(SELECT account_id FROM {bots}.playerbots_account_type)", "characters")
        st["online"] = sorted(r[0] for r in rows if r[0] not in own)
    except Exception as e:  # noqa: BLE001
        st["error"] = f"{type(e).__name__}: {e}"
    return {"state": st, "duration_s": round(time.perf_counter() - t0, 3)}


def run_test(cid: str, ident: S.Identity, base: dict) -> dict:
    reg = load_all()
    rec = {"id": cid, "identity": ident.user}
    t0 = time.time()
    with S.as_identity(ident):
        try:
            t1 = time.time()
            S.ensure()
            # a character that never entered the world has no zone yet (characters.zone 0 until the first login):
            # one login and logout each makes a fresh character look like a played one
            # (and some money: a fresh character can't pay a mail's postage, which saves.item_ids_fresh needs)
            for n in (ident.char, ident.alt):
                with S.Session(n, tele=None) as s:
                    s.ws.set_selection(s.guid)
                    s.cmd(".modify money 10000")
            rec["identity_s"] = round(time.time() - t1, 2)
            reg[cid].fn({})
            rec["status"] = "pass"
        except CheckFail as e:
            rec |= {"status": "fail", "message": str(e)[:300]}
        except Exception as e:  # noqa: BLE001
            rec |= {"status": "error", "message": f"{type(e).__name__}: {e}"[:300]}
        # the test's own characters must be offline before the reset (a pooled sibling's may be online)
        for n in (ident.char, ident.alt):
            if S.char_row(n):
                S.wait_offline(n, timeout=30)
    rec["duration_s"] = round(time.time() - t0, 2)
    return rec


def run_slice(ids: list[str], workers: int = 1, run_tag: int | None = None) -> dict:
    run_tag = run_tag if run_tag is not None else int(time.time()) % 100000
    S.before_writes()                         # the PLAYER baseline and safety copy, once, before any worker
    base = shared_state()
    idents = {cid: new_identity(run_tag) for cid in ids}
    own_all = {n for i in idents.values() for n in (i.char, i.alt)}
    records, resets = [], []
    t0 = time.time()

    def one(cid):
        rec = run_test(cid, idents[cid], base)
        r = shared_state(own_all)
        resets.append(r["duration_s"])
        if r["state"] != base["state"]:
            rec["leaked"] = {"expected": base["state"], "actual": r["state"]}
            if rec["status"] == "pass":
                rec["status"] = "fail"
                rec["message"] = "leaked state"
        return rec

    if workers <= 1:
        records = [one(c) for c in ids]
    else:
        with ThreadPoolExecutor(workers) as ex:
            records = list(ex.map(one, ids))
    wall = round(time.time() - t0, 2)
    cleanup = cleanup_identities(list(idents.values()))
    return {"workers": workers, "order": ids, "wall_s": wall, "records": records,
            "resets_s": resets, "reset_median_s": round(statistics.median(resets), 3) if resets else None,
            "reset_max_s": max(resets) if resets else None, "base_state": base, "cleanup": cleanup}


def cleanup_identities(idents: list[S.Identity]) -> dict:
    """Erase the run's characters and accounts through the server and drop their save folders."""
    t0 = time.time()
    errs = []
    for i in idents:
        if not i.user.startswith("HSI"):
            errs.append(f"refused: {i.user} isn't a fixture account")
            continue
        for n in (i.char, i.alt):
            if S.char_row(n):
                r = soap.gm(f"character erase {n}", timeout=15)
                if not r.ok:
                    errs.append(f"erase {n}: {(r.output or '')[:120]}")
        if db.scalar(f"SELECT id FROM account WHERE username='{i.user}'", "auth"):
            r = soap.gm(f"account delete {i.user}", timeout=15)
            if not r.ok:
                errs.append(f"account delete {i.user}: {(r.output or '')[:120]}")
        d = P.SAVES / i.user
        if d.is_dir() and d.name.startswith("HSI"):
            shutil.rmtree(d, ignore_errors=True)
    return {"duration_s": round(time.time() - t0, 2), "errors": errs}


# ---------------------------------------------------------------- bulk fixtures (design 13 rule 4; task 1.6)

def prepare_identity(ident: S.Identity) -> dict:
    """Account + two characters through the server, then one login/logout each with postage money (a fresh
    character has no zone until its first login, and a mail needs 30 copper)."""
    t0 = time.time()
    with S.as_identity(ident):
        S.ensure()
        for n in (ident.char, ident.alt):
            with S.Session(n, tele=None) as s:
                s.ws.set_selection(s.guid)
                s.cmd(".modify money 10000")
    return {"identity": ident.user, "s": round(time.time() - t0, 2)}


def bulk_identities(n: int, workers: int = 8, run_tag: int | None = None) -> dict:  # noqa: D401
    """n identities created in parallel at run start; {"identities", "wall_s", "each_s"}."""
    run_tag = run_tag if run_tag is not None else int(time.time()) % 100000
    ids = [new_identity(run_tag) for _ in range(n)]
    t0 = time.time()

    def safe(i):
        # fail closed per identity: a setup error is reported for the test that would use it, never a crash
        t1 = time.time()
        try:
            return prepare_identity(i)
        except Exception as e:  # noqa: BLE001
            return {"identity": i.user, "s": round(time.time() - t1, 2), "error": f"{type(e).__name__}: {e}"[:300]}
    with ThreadPoolExecutor(workers) as ex:
        each = list(ex.map(safe, ids))
    return {"identities": ids, "wall_s": round(time.time() - t0, 2), "each_s": [e["s"] for e in each],
            "workers": workers, "failed": {e["identity"]: e["error"] for e in each if e.get("error")}}


def cleanup_stale() -> dict:
    """Fixture identities left by an earlier run that was killed before its own cleanup (accounts HSI*): their
    characters and accounts are removed through the server, and their save folders deleted. Run at fixture start."""
    t0 = time.time()
    rows = db.query("SELECT id, username FROM account WHERE username LIKE 'HSI%'", "auth")
    idents = []
    for aid, user in rows:
        chars = [r[0] for r in db.query(f"SELECT name FROM characters WHERE account = {int(aid)} ORDER BY guid", "characters")]
        chars = (chars + ["", ""])[:2]
        idents.append(S.Identity(user, "", chars[0] or "Hsnone", chars[1] or "Hsnone"))
    out = cleanup_identities(idents) if idents else {"errors": []}
    return {"stale_accounts": [u for _, u in rows], "duration_s": round(time.time() - t0, 2), "errors": out.get("errors")}
