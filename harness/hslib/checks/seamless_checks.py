"""seamless-experience: nothing the player sees says server, realm, connecting or account
(openspec/changes/seamless-experience; login-screen, client-connectivity).

Runtime checks drive the real ./play with the real Wow.exe against a scratch play client (playclient_util.PlayRun) as
the harness test account HSTEST, like the in-client-login checks. Evidence:
- the front menu's test record (HSSeamless.lua, HS_TEST only): every intercepted stock dialog, overlay change, error
  action and the character-select inspection, handed out through the client's own settings file. Just before
  EnterWorld the glue code appends "HSLOG<record>|" to the saved account list (accountList), which the client writes
  to WTF/Config.wtf when it enters the world;
- the client's own screenshots (marker squares, HSFrontMenu_UpdateMarker; the fifth square is green while the loading
  overlay is visible, blue while the friendly error is) and their pixels;
- the wording add-on's report in its SavedVariables (WTF/Account/HSTEST/SavedVariables/Moonglade.lua, written by the
  client at logout; the hook is planted by the check into the scratch play client only);
- the server's own log (Server.log "Login Character:[<name>]") and the client's Logs/connection.log;
- the stock strings, read from the installed client's own GlueStrings.lua / GlobalStrings.lua (client/, `hs mpq`).

client.login_text_clean drives the headless protocol probe against the running stack (no client).
"""
from __future__ import annotations

import json
import os
import re
import signal
import time
import zlib
from pathlib import Path

from .. import clientbuild, mpq, paths as P, stack
from ..registry import check, require
from .play_checks import is_clear, remaining, require_given_clear
from .playclient_util import (PlayRun, _bmp_pixels, _classify, connection_log, drop_scratch, fresh_scratch,
                              glue_logs, marker, screenshots)

CAP_LOGIN = "login-screen"
CAP_CONN = "client-connectivity"

LOG_TIMEOUT = 240            # Continue at 20 s + login + character select + Play + entering the world, generous
REPORT_TIMEOUT = 240         # + the add-on's logout (8 s in the world, the 20 s logout countdown, back to the glue)
FORBIDDEN_FRIENDLY = ("server", "realm", "connect", "authenticat", "account")   # spec, login-screen friendly errors
# words that make a string server-flavoured (the add-on's replacements must not keep them)
SERVER_FLAVOUR = ("logout", "log out", "logged out", "server", "disconnect", "connection", "realm")

CONTINUE_AT = 20
# Run B (shared by login.loading_overlay, login.charselect_wording, client.wording_addon): menu shot at 4 s; Continue
# at 20 s with a shot in the same frame (the overlay is up); at character select the wording is recorded, a shot is
# taken, and Play is clicked; in the world the add-on logs out after 8 s (its test hook).
GLUE_PLAY = {"marker": True,
             "script": [{"t": 4, "act": "shot"}, {"t": CONTINUE_AT, "act": "continue"}, {"t": CONTINUE_AT, "act": "shot"}],
             "charselect": [{"t": 1.5, "act": "csinspect"}, {"t": 2, "act": "shot"}, {"t": 4, "act": "play"}]}
ADDON_HOOK = '-- planted by the harness (seamless_checks.py): the wording add-on\'s test hook\r\nMoongladeTest = {\r\n\t["hook"] = 1,\r\n\t["logout_after"] = 8,\r\n}\r\n'
# Run A (login.friendly_errors): the login server is stopped before Continue; the friendly error is photographed,
# its details opened and photographed; the login server is started again; Try again at RETRY_AT; at character select
# Play is clicked (entering the world hands the record out).
RETRY_AT = 80
GLUE_ERR = {"marker": True,
            "script": [{"t": 4, "act": "shot"}, {"t": CONTINUE_AT, "act": "continue"}, {"t": 32, "act": "shot"},
                       {"t": 34, "act": "details"}, {"t": 35, "act": "shot"}, {"t": RETRY_AT, "act": "retry"}],
            "charselect": [{"t": 2, "act": "shot"}, {"t": 4, "act": "play"}]}

_SESSIONS: dict = {}


# ---------------------------------------------------------------- evidence helpers

def config_value(client_dir: Path, key: str) -> str | None:
    p = client_dir / "WTF" / "Config.wtf"
    m = re.search(rf'^SET {key} "([^"]*)"', p.read_text(errors="replace"), re.M) if p.exists() else None
    return m.group(1) if m else None


def glue_record(client_dir: Path) -> list[str] | None:
    """The front menu's compact test record (HSSeamless_FlushLog; token list in HSSeamless.lua's header) from
    WTF/Config.wtf, as a list of tokens; None if absent."""
    v = config_value(client_dir, "accountList")
    m = re.search(r"HSLOG([^|]*)\|", v or "")
    return m.group(1).split(";") if m else None


def adler(s: str | bytes) -> str:
    """HSSeamless_Adler: Adler-32 of the UTF-8 bytes as 8 hex digits."""
    return format(zlib.adler32(s.encode("utf-8") if isinstance(s, str) else s), "08x")


def friendly_source() -> dict[str, str]:
    """HS_FRIENDLY from the front menu's source (client-mods/glue/HSSeamless.lua)."""
    text = (P.ROOT / "client-mods" / "glue" / "HSSeamless.lua").read_text()
    block = re.search(r"HS_FRIENDLY = \{(.*?)\n\};", text, re.S).group(1)
    return {m.group(1): _lua_unescape(m.group(2)) for m in re.finditer(r'(\w+) = "((?:[^"\\]|\\.)*)",', block)}


def record_ok(rec: list[str] | None) -> dict:
    """Whole-record sanity: present, not cut at the cvar limit, ends with the hand-out and the Lua error count."""
    out = {"present": bool(rec)}
    if rec:
        out |= {"truncated": rec[-1].endswith("~"), "handed_out": "W" in rec,
                "lua_errors": rec[-1][1:] if rec[-1].startswith("E") else None}
    return out


def tok(rec: list[str], prefix: str, start: int = 0) -> int | None:
    """Index of the first token at or after `start` that equals `prefix` or starts with `prefix` + ":"."""
    for i in range(start, len(rec)):
        if rec[i] == prefix or rec[i].startswith(prefix + ":"):
            return i
    return None


def _lua_unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n.isdigit():
                j = i + 1
                while j < len(s) and j < i + 4 and s[j].isdigit():
                    j += 1
                out.append(chr(int(s[i + 1:j])))
                i = j
                continue
            out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'"}.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def stock_strings(inside: str) -> dict[str, str]:
    """NAME -> value of every `NAME = "value";` line of a client string file, read from the installed client's own
    archives (client/, highest priority first; clientbuild.effective_file)."""
    data, src = clientbuild.effective_file(P.CLIENT, inside)
    text = data.decode("utf-8", errors="replace")
    out = {}
    for m in re.finditer(r'^([A-Z][A-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*;?\s*$', text, re.M):
        out[m.group(1)] = _lua_unescape(m.group(2))
    out["__source__"] = src
    return out


def clean_field(s: str) -> str:
    """The test record's field transform (HSSeamless.lua clean()), applied to a stock string to compare it."""
    s = re.sub(r"[\r\n]", " ", s)
    return s.replace("|", "/").replace('"', "'").replace(";", ",")


def plain_text(s: str) -> str:
    """HSSeamless_PlainText: what the details line shows for a stock message."""
    s = s.replace("|n", "\n")
    s = re.sub(r"<[^>]*>", "", s)
    return s.replace("&amp;", "&")


class LuaParseError(ValueError):
    pass


def parse_saved_variables(text: str) -> dict:
    """The client's SavedVariables file format (NAME = value lines; tables as {["key"] = value, [1] = value, ...})."""
    i, n = 0, len(text)

    def ws():
        nonlocal i
        while i < n:
            if text[i] in " \t\r\n,;":
                i += 1
            elif text.startswith("--", i):
                j = text.find("\n", i)
                i = n if j < 0 else j + 1
            else:
                break

    def value():
        nonlocal i
        ws()
        if text.startswith("{", i):
            i += 1
            t, arr = {}, []
            while True:
                ws()
                if i >= n:
                    raise LuaParseError("unterminated table")
                if text[i] == "}":
                    i += 1
                    return t if t or not arr else arr
                if text[i] == "[":
                    i += 1
                    k = value()
                    ws()
                    if text[i] != "]":
                        raise LuaParseError(f"expected ] at {i}")
                    i += 1
                    ws()
                    if text[i] != "=":
                        raise LuaParseError(f"expected = at {i}")
                    i += 1
                    t[k] = value()
                else:
                    m = re.match(r"[A-Za-z_]\w*\s*=", text[i:])
                    if m:
                        k = m.group(0)[:-1].strip()
                        i += m.end()
                        t[k] = value()
                    else:
                        arr.append(value())
        if text.startswith('"', i):
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    buf.append(text[j:j + 2])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            i = j + 1
            return _lua_unescape("".join(buf))
        for lit, v in (("true", True), ("false", False), ("nil", None)):
            if text.startswith(lit, i):
                i += len(lit)
                return v
        m = re.match(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", text[i:])
        if m:
            i += m.end()
            return float(m.group(0)) if (m.group(1) or m.group(2)) else int(m.group(0))
        raise LuaParseError(f"unexpected {text[i:i + 20]!r} at {i}")

    out = {}
    while True:
        ws()
        if i >= n:
            return out
        m = re.match(r"([A-Za-z_]\w*)\s*=", text[i:])
        if not m:
            raise LuaParseError(f"expected NAME = at {i}: {text[i:i + 30]!r}")
        i += m.end()
        out[m.group(1)] = value()


def addon_sv_path(client_dir: Path) -> Path:
    return client_dir / "WTF" / "Account" / P.TEST_USER / "SavedVariables" / "Moonglade.lua"


def plant_addon_hook(client_dir: Path) -> dict:
    """The wording add-on's test hook, into a scratch play client only (never env/client-play/, never client/)."""
    require(client_dir.parent == P.ENV and client_dir.name.startswith("check-play-"),
            "refusing to plant the add-on test hook outside a scratch play client", dir=str(client_dir))
    p = addon_sv_path(client_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(ADDON_HOOK)
    return {"planted": str(p.relative_to(client_dir))}


def addon_report(client_dir: Path) -> dict | None:
    p = addon_sv_path(client_dir)
    if not p.exists():
        return None
    try:
        sv = parse_saved_variables(p.read_text(errors="replace"))
    except LuaParseError as e:
        return {"parse_error": str(e), "raw": p.read_text(errors="replace")[:2000]}
    t = sv.get("MoongladeTest")
    return t.get("report") if isinstance(t, dict) else None


def server_logins(name: str) -> list[dict]:
    """Character logins in the server's own log: Players.log (config.py; CharacterHandler.cpp:1067 "Login
    Character:[name]", time-stamped "%Y-%m-%d %H:%M:%S", local time), as {t (epoch), line}."""
    p = P.LOGS / "Players.log"
    if not p.exists():
        return []
    out = []
    for l in p.read_text(errors="replace").splitlines():
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$", l)
        if m and f"Login Character:[{name}]" in m.group(2):
            out.append({"t": time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")), "line": m.group(2)[:160]})
    return out


def black_fraction(path: Path) -> dict:
    """Share of near-black pixels on a grid over the screenshot, leaving out the overlay's own drawings (logo at the
    top, caption and loading bar in the middle band) and the test marker (top right)."""
    w, h, px = _bmp_pixels(path)
    pts = black = 0
    for gy in range(1, 20):
        for gx in range(1, 20):
            x, y = int(w * gx / 20), int(h * gy / 20)
            if y < h * 0.30 or (0.40 * h < y < 0.62 * h) or (x > w - 260 * h / 768 and y < 120 * h / 768):
                continue
            r, g, b = px(x, y)
            pts += 1
            black += max(r, g, b) < 24
    return {"points": pts, "black": black, "fraction": round(black / pts, 3) if pts else 0.0}


def marker_overlay(path: Path) -> str:
    """The fifth marker square (second row, HSTestMarkerOverlay): green / blue / white."""
    w, h, px = _bmp_pixels(path)
    u = h / 768
    cx = round(w - (10 + 200) * u + 20 * u)
    cy = round((10 + 53 + 20) * u)
    r = max(3, int(6 * u))
    acc = [0, 0, 0]
    for x in range(cx - r, cx + r + 1):
        for y in range(cy - r, cy + r + 1):
            p = px(x, y)
            for i in range(3):
                acc[i] += p[i]
    nn = (2 * r + 1) ** 2
    return _classify(tuple(round(v / nn) for v in acc))


def shot_info(path: Path) -> dict:
    m = marker(path)
    return {"file": path.name, "mtime": round(path.stat().st_mtime, 3), "lua_errors": m["lua_errors"],
            "main_menu_up": m["main_menu_up"], "overlay_square": marker_overlay(path), "black": black_fraction(path)}


def _wait(pred, timeout, step=1.0):
    t = time.time()
    while time.time() - t < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def _finish(run: PlayRun, ev: dict) -> None:
    ev["stop"] = run.stop()
    rec = run.record()
    ev["play_record"] = {k: rec.get(k) for k in ("outcome", "exit_code")}
    ev["remaining_after"] = remaining()


def _session_ok(key: str) -> dict:
    ev = _SESSIONS[key]
    require(not ev.get("session_error"), f"the shared {key} run failed: {ev.get('session_error')}", **ev)
    return ev


def _hide(t: str) -> dict:
    """H:<why>:<frames>:<overlay frames>:<stock dialog frames>:<intercepted>"""
    f = t.split(":")
    if len(f) != 6 or f[0] != "H":
        return {}
    return {"why": f[1], "frames": int(f[2]), "overlay": int(f[3]), "dialog": int(f[4]), "icpt": int(f[5])}


def _overlay_phase(rec: list[str], start: int) -> dict:
    """The overlay phase that starts at rec[start] ("O:..."): its tokens up to its "H:" and the parsed counts."""
    end = tok(rec, "H", start)
    seg = rec[start:end + 1] if end is not None else rec[start:]
    return {"tokens": seg, "hide": _hide(seg[-1]) if end is not None else {},
            "intercepted": [t for t in seg if t.startswith("I:")], "visible": [t for t in seg if t.startswith("V:")],
            "end": end}


# ---------------------------------------------------------------- run B: Continue, character select, Play, the world

def play_session() -> dict:
    """One ./play run (memoised): Continue -> overlay -> character select (inspected) -> Play -> world -> the add-on
    logs out. Evidence for login.loading_overlay, login.charselect_wording and client.wording_addon."""
    if "play" in _SESSIONS:
        return _SESSIONS["play"]
    ev: dict = {"account": P.TEST_USER, "glue_test": GLUE_PLAY,
                "shared_run": "seamless play (login.loading_overlay + login.charselect_wording + client.wording_addon)"}
    _SESSIONS["play"] = ev
    scratch = run = None
    try:
        require_given_clear(ev)
        scratch = fresh_scratch("seamless")
        build = clientbuild.build(dest=scratch)
        ev["scratch"] = {"dir": str(scratch), "outputs": build["stamp"]["outputs"]}
        ev["addon_hook"] = plant_addon_hook(scratch)
        run = PlayRun("seamless", glue_test=GLUE_PLAY, scratch=scratch)
        run.start()
        up = run.wait_client()
        ev["client_up"] = {k: v for k, v in up.items() if k != "t"}
        require(up.get("up"), "the real Wow.exe never came up", output=run.output()[-2500:], **ev)
        t_up = up["t"]
        rec = _wait(lambda: glue_record(scratch), LOG_TIMEOUT, step=2)
        ev["glue_record"] = rec
        rep = _wait(lambda: addon_report(scratch), REPORT_TIMEOUT, step=2) if rec else None
        ev["addon_report"] = rep
        ev["frame_log"] = glue_logs(scratch)
        shots = [p for p in screenshots(scratch) if p.stat().st_mtime >= t_up - 5]
        ev["screens"] = [shot_info(p) for p in shots]
        ev["t_client_up"] = round(t_up, 3)
        ev["connection_log"] = connection_log(scratch)[-1500:]
        ev["addon_files"] = sorted(str(p.relative_to(scratch)) for p in (scratch / "Interface" / "AddOns" / "Moonglade").glob("*"))
        _finish(run, ev)
        # the server's log after it stopped (complete; it's rewritten only at the next start). The fixture step's own
        # probe logins happen before the client starts, so a login after the client came up is the client's.
        logins = server_logins(P.TEST_CHAR)
        ev["server_logins"] = logins
        ev["server_logins_after_client_up"] = [x for x in logins if x["t"] >= int(t_up)]
        return ev
    except Exception as e:  # noqa: BLE001  (recorded for all three checks; the first caller re-raises)
        ev["session_error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        if run is not None:
            run.cleanup()
        ev["scratch_frame_xml"] = framexml_untouched(scratch, ev.get("scratch", {}).get("outputs") or {}) if scratch and scratch.exists() else None
        drop_scratch(scratch)


def framexml_untouched(scratch: Path, outputs: dict) -> dict:
    """No file under Interface/FrameXML differs from the stock client's: loose files compared with client/'s, and no
    archive the build added holds an Interface\\FrameXML\\ entry."""
    def tree(root: Path) -> dict:
        d = root / "Interface" / "FrameXML"
        return {str(p.relative_to(d)): clientbuild.sha256_file(p) for p in d.rglob("*") if p.is_file()} if d.is_dir() else {}
    loose_play, loose_stock = tree(scratch), tree(P.CLIENT)
    archives = {}
    for rel in outputs:
        if rel.lower().endswith(".mpq"):
            with mpq.Archive(scratch / rel) as a:
                archives[rel] = [n for n in a.names() if n.lower().startswith("interface\\framexml\\")]
    return {"loose_differs": sorted(k for k in set(loose_play) | set(loose_stock) if loose_play.get(k) != loose_stock.get(k)),
            "loose_files": len(loose_play), "archives_framexml_entries": archives,
            "added_outputs": sorted(outputs)}


@check("login.loading_overlay", order=391, capability=CAP_LOGIN, stack="down", tier="full")
def loading_overlay(ctx):
    """Continue on a scratch patched play client: the "Entering Azeroth..." overlay is shown from the Continue press
    until character select (visible in every frame of that time), the client shows no stock connection dialog (each
    attempt is intercepted and recorded by the test hook, at least one was), there are no Lua errors, and the
    screenshot taken in the Continue frame shows the overlay (marker square and an almost all-black screen)."""
    if "play" not in _SESSIONS:
        play_session()
    s = _session_ok("play")
    rec = s.get("glue_record") or []
    ev = {k: s.get(k) for k in ("glue_record", "screens", "frame_log", "client_up", "remaining_after", "shared_run")}
    ev["record"] = record_ok(rec)
    require(rec and not ev["record"]["truncated"], "no complete test record came out of the client", **ev)
    i_c = tok(rec, "C")
    require(i_c is not None and i_c + 1 < len(rec), "the record has no Continue press", **ev)
    caption = "Entering Azeroth\u2026"
    require(rec[i_c + 1] == f"O:continue:{adler(caption)}",
            f"the {caption!r} overlay wasn't shown at the Continue press", **ev)
    ph = _overlay_phase(rec, i_c + 1)
    ev["phase"] = ph
    h = ph["hide"]
    require(h.get("why") == "charlist", "the overlay didn't stay until the character list arrived", **ev)
    lists = [t for t in ph["tokens"] + rec[ph["end"] + 1:ph["end"] + 2] if re.fullmatch(r"L\d+", t)]
    require(lists and int(lists[-1][1:]) >= 1, "the overlay didn't end with character select's list", **ev)
    require(h["frames"] > 0 and h["overlay"] == h["frames"],
            "the overlay wasn't visible in every frame between Continue and character select", **ev)
    require(ph["intercepted"] and h["icpt"] >= 1 and all(t == "I:CANCEL" for t in ph["intercepted"]),
            "the test hook didn't intercept the stock connection dialogs (nothing recorded)", **ev)
    require(h["dialog"] == 0 and not ph["visible"], "a stock dialog was visible between Continue and character select",
            **ev)
    require(ev["record"]["lua_errors"] == "0" and s["screens"] and all(x["lua_errors"] == 0 for x in s["screens"]),
            "Lua errors (test error handler)", **ev)
    require(not any((v or "").strip() for v in s["frame_log"].values()), "GlueXML.log / FrameXML.log hold errors", **ev)
    # the screenshot taken in the Continue frame: the run's second screenshot ("S" right after the overlay showed)
    require("S" in ph["tokens"] and rec[:i_c].count("S") == 1,
            "the connecting screenshot wasn't taken while the overlay was up", **ev)
    require(len(s["screens"]) >= 3, "the client didn't take the menu, connecting and character-select screenshots",
            **ev)
    menu, connecting = s["screens"][0], s["screens"][1]
    ev["connecting_shot"] = connecting
    require(connecting["overlay_square"] == "green", "the connecting screenshot's marker doesn't show the overlay", **ev)
    require(connecting["black"]["fraction"] >= 0.9 and menu["black"]["fraction"] < 0.9,
            "the connecting screenshot isn't the overlay (an almost all-black screen, unlike the menu's)", **ev)
    require(is_clear(s["remaining_after"]), "something is still running", **ev)
    return ev


@check("login.charselect_wording", order=392, capability=CAP_LOGIN, stack="down", tier="full")
def charselect_wording(ctx):
    """At character select (test account, its one character) the test hook records the realm name and Change Realm
    hidden and the buttons reading "Play" and "Main Menu"; clicking Play (the button's own click) enters the world:
    EnterWorld runs and the server log shows the character's login after the client started."""
    if "play" not in _SESSIONS:
        play_session()
    s = _session_ok("play")
    rec = s.get("glue_record") or []
    ev = {k: s.get(k) for k in ("glue_record", "t_client_up", "server_logins", "server_logins_after_client_up",
                                "remaining_after", "shared_run")}
    ev["record"] = record_ok(rec)
    require(rec and not ev["record"]["truncated"], "no complete test record came out of the client", **ev)
    cs = [t for t in rec if t.startswith("Q:")]
    ev["inspection"] = cs
    require(len(cs) == 1, "the test hook didn't inspect character select once", **ev)
    # the character list as it stood at the inspection (the client may send an empty update first)
    lists = [t for t in rec[:rec.index(cs[0])] if re.fullmatch(r"L\d+", t)]
    ev["charlist"] = lists
    require(lists and lists[-1] == "L1", "GIVEN not met: character select didn't list the test account's one "
            "character", **ev)
    f = cs[0].split(":")
    ev["fields"] = dict(zip(("Q", "screen", "realm_shown", "change_shown", "play", "back", "play_shown", "overlay"), f))
    require(len(f) == 8 and f[1] == "charselect", "the inspection wasn't on character select", **ev)
    require(f[2] == "0" and f[3] == "0", "the realm name or Change Realm is shown", **ev)
    require(f[4] == "Play" and f[5] == "Main Menu" and f[6] == "1", "the buttons don't read Play and Main Menu", **ev)
    i_p = tok(rec, "P")
    require(i_p is not None and rec[i_p] == "P:Play" and rec[i_p + 1:i_p + 2] == ["W"],
            "clicking Play didn't run EnterWorld", **ev)
    require(s["server_logins_after_client_up"], "the server log shows no login of the character after Play", **ev)
    require(is_clear(s["remaining_after"]), "something is still running", **ev)
    return ev


def _addon_wording() -> dict[str, str]:
    """The replacements, read from the add-on's source (client-mods/addons/Moonglade/Moonglade.lua)."""
    text = (P.ROOT / "client-mods" / "addons" / "Moonglade" / "Moonglade.lua").read_text()
    block = re.search(r"local WORDING = \{(.*?)\n\};", text, re.S).group(1)
    return {m.group(1): _lua_unescape(m.group(2)) for m in re.finditer(r'(\w+) = "((?:[^"\\]|\\.)*)",', block)}


@check("client.wording_addon", order=393, capability=CAP_CONN, stack="down", tier="full")
def wording_addon(ctx):
    """A test character enters the world on a scratch patched play client and logs out: the add-on's report (its
    SavedVariables, written at logout) shows it loaded and enabled, each listed global string (Logout and its
    countdown, the disconnect / lost-connection lines) with its new value in place and the original equal to the
    client's own GlobalStrings.lua, the countdown popup showing the new text, 0 Lua errors; no file under
    Interface/FrameXML differs from the stock client's."""
    if "play" not in _SESSIONS:
        play_session()
    s = _session_ok("play")
    rep = s.get("addon_report")
    ev = {k: s.get(k) for k in ("addon_report", "addon_hook", "addon_files", "frame_log", "scratch_frame_xml",
                                "remaining_after", "shared_run")}
    require(isinstance(rep, dict) and not rep.get("parse_error"), "no add-on report (it never logged out, or the "
            "SavedVariables file didn't parse)", **ev)
    require(rep.get("at") == "PLAYER_LOGOUT" and rep.get("loaded") == 1 and rep.get("enabled") == 1,
            "the add-on wasn't loaded and enabled", **ev)
    want = _addon_wording()
    stock = stock_strings("Interface\\FrameXML\\GlobalStrings.lua")
    ev["stock_source"] = stock["__source__"]
    required = {"LOGOUT", "CAMP_TIMER", "IDLE_MESSAGE", "ERR_COMSAT_DISCONNECT"}
    strings = rep.get("strings") or {}
    table = {}
    for name, new in want.items():
        got = strings.get(name) or {}
        table[name] = {"stock": stock.get(name), "original_reported": got.get("original"), "new": new,
                       "now": got.get("now"),
                       "format_kept": re.findall(r"%\w", stock.get(name) or "") == re.findall(r"%\w", new),
                       "flavour_left": [w for w in SERVER_FLAVOUR if w in new.lower()]}
    ev["strings"] = table
    require(required <= set(want) and set(strings) == set(want), "the report doesn't list every replaced string "
            f"(at least {sorted(required)})", **ev)
    bad = {k: v for k, v in table.items() if not (v["stock"] is not None and v["original_reported"] == v["stock"]
                                                   and v["now"] == v["new"] != v["stock"] and v["format_kept"]
                                                   and not v["flavour_left"])}
    require(not bad, "a string isn't replaced as intended (original from the client, new value in place, same "
            f"format, no server wording): {sorted(bad)}", **ev)
    ui = rep.get("ui") or {}
    ev["ui"] = ui
    camp_re = "^" + re.escape(want["CAMP_TIMER"]).replace("%d", r"\d+").replace("%s", r"\S+") + "$"
    require(ui.get("logout_button") == want["LOGOUT"] and ui.get("camp_popup_template") == want["CAMP_TIMER"]
            and re.match(camp_re, ui.get("camp_popup_shown") or ""),
            "the game menu button or the logout countdown doesn't show the new wording", **ev)
    require(rep.get("errors") == 0, "the add-on's test hook counted Lua errors", **ev)
    require(not any((v or "").strip() for v in s["frame_log"].values()), "GlueXML.log / FrameXML.log hold errors", **ev)
    fx = s.get("scratch_frame_xml") or {}
    require(fx and not fx["loose_differs"] and not any(fx["archives_framexml_entries"].values()),
            "a file under Interface/FrameXML differs from the stock client's", **ev)
    require(any(r.startswith("Interface/AddOns/Moonglade/") for r in fx.get("added_outputs", [])),
            "the add-on isn't part of the play client build", **ev)
    require(is_clear(s["remaining_after"]), "something is still running", **ev)
    return ev


# ---------------------------------------------------------------- run A: login server down, friendly error, Try again

def _auth_pids() -> list[int]:
    return stack.stack_processes()["authserver"]


def error_session() -> dict:
    if "error" in _SESSIONS:
        return _SESSIONS["error"]
    ev: dict = {"account": P.TEST_USER, "glue_test": GLUE_ERR}
    _SESSIONS["error"] = ev
    scratch = run = None
    try:
        require_given_clear(ev)
        scratch = fresh_scratch("seamlesserr")
        clientbuild.build(dest=scratch)
        run = PlayRun("seamlesserr", glue_test=GLUE_ERR, scratch=scratch)
        run.start()
        up = run.wait_client()
        ev["client_up"] = {k: v for k, v in up.items() if k != "t"}
        require(up.get("up"), "the real Wow.exe never came up", output=run.output()[-2500:], **ev)
        t_up = up["t"]
        # GIVEN: the login server is stopped (SIGTERM, then verified) before the Continue at 20 s
        pids = _auth_pids()
        for pid in pids:
            os.kill(pid, signal.SIGTERM)
        gone = _wait(lambda: not _auth_pids() and not stack.port_open(P.AUTH_PORT), 15, step=0.2)
        ev["auth_stopped"] = {"pids": pids, "stopped": bool(gone), "after_client_up_s": round(time.time() - t_up, 1)}
        require(pids and gone, "GIVEN not met: couldn't stop the login server", **ev)
        # the friendly error and its details are photographed (32 s and 35 s); then the login server comes back
        _wait(lambda: len([p for p in screenshots(scratch) if p.stat().st_mtime >= t_up - 5]) >= 3, 120, step=1)
        ev["auth_down_at_details_shot"] = not stack.port_open(P.AUTH_PORT) and not _auth_pids()
        ev["auth_restart"] = stack.auth_up(timeout=30)
        ev["auth_restart"]["after_client_up_s"] = round(time.time() - t_up, 1)
        rec = _wait(lambda: glue_record(scratch), LOG_TIMEOUT, step=2)
        ev["glue_record"] = rec
        shots = [p for p in screenshots(scratch) if p.stat().st_mtime >= t_up - 5]
        ev["screens"] = [shot_info(p) for p in shots]
        ev["frame_log"] = glue_logs(scratch)
        ev["connection_log"] = connection_log(scratch)[-3000:]
        _finish(run, ev)
        return ev
    except Exception as e:  # noqa: BLE001
        ev["session_error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        if run is not None:
            run.cleanup()
        drop_scratch(scratch)


@check("login.friendly_errors", order=394, capability=CAP_LOGIN, stack="down", tier="full")
def friendly_errors(ctx):
    """Login server stopped, Continue: the friendly error appears (no server/realm/connect/authenticat/account in its
    message), with Try again / Back to menu; Show details reveals the client's own stock error text (a GlueStrings.lua
    message, shown as plain text); after the login server is started again, Try again reaches character select."""
    if "error" not in _SESSIONS:
        error_session()
    s = _session_ok("error")
    rec = s.get("glue_record") or []
    ev = {k: s.get(k) for k in ("glue_record", "screens", "auth_stopped", "auth_down_at_details_shot", "auth_restart",
                                "frame_log", "remaining_after", "connection_log")}
    ev["record"] = record_ok(rec)
    require(rec and not ev["record"]["truncated"], "no complete test record came out of the client (Try again never "
            "reached the world)", **ev)
    require(s["auth_stopped"]["stopped"] and s["auth_down_at_details_shot"],
            "GIVEN not met: the login server wasn't down from the Continue to the details", **ev)
    # the client's own log: the Continue's attempt found the login server down, the later one got through
    conn = s["connection_log"] or ""
    down, up = conn.find("result: LOGIN_SERVER_DOWN"), conn.find("RESPONSE_CONNECTED")
    ev["connection_log_down_then_up"] = [down, up]
    require(0 <= down < up, "GIVEN not met: the client's connection.log doesn't show the login server down at the "
            "Continue and reached after Try again", **ev)
    i_c, i_x = tok(rec, "C"), tok(rec, "X")
    require(i_c is not None and i_x is not None and i_c < i_x, "no friendly error followed the Continue", **ev)
    ph = _overlay_phase(rec, i_c + 1)
    ev["first_phase"] = ph
    require(rec[i_c + 1].startswith("O:continue:") and ph["intercepted"] and ph["hide"].get("why") == "error"
            and ph["end"] + 1 == i_x and not ph["visible"],
            "the error didn't come from the Continue (overlay, an intercepted stock dialog, then the error)", **ev)
    x = rec[i_x].split(":")
    require(len(x) == 7, "the friendly error's token is malformed", **ev)
    _, which, kind, raw_len, raw_adler, title_adler, text_adler = x
    friendly = friendly_source()
    ev["error"] = {"stock_dialog": which, "kind": kind, "title": friendly["login_title"], "text": friendly["login_text"],
                   "title_matches_source": title_adler == adler(friendly["login_title"]),
                   "text_matches_source": text_adler == adler(friendly["login_text"])}
    words = [w for w in FORBIDDEN_FRIENDLY if w in (friendly["login_title"] + " " + friendly["login_text"]).lower()]
    ev["forbidden_in_message"] = words
    require(kind == "login" and ev["error"]["title_matches_source"] and ev["error"]["text_matches_source"],
            "the friendly error didn't show the login message", **ev)
    require(not words, "the friendly message uses server wording", **ev)
    require(friendly["retry"] == "Try again" and friendly["back"] == "Back to menu",
            "the buttons aren't Try again / Back to menu", **ev)
    # the details: the stock text the client would have shown
    gs = stock_strings("Interface\\GlueXML\\GlueStrings.lua")
    keys = sorted(k for k, v in gs.items() if k != "__source__"
                  and str(len(v.encode("utf-8"))) == raw_len and adler(v) == raw_adler)
    ev["stock_keys_matching_raw"] = keys
    ev["glue_strings_source"] = gs["__source__"]
    require(keys, "the error the client reported isn't one of its own GlueStrings messages", **ev)
    i_d = tok(rec, "D", i_x)
    ev["details_token"] = rec[i_d] if i_d is not None else None
    shown = plain_text(gs[keys[0]])
    ev["details_expected"] = {"key": keys[0], "text": shown, "len": len(shown.encode("utf-8")), "adler": adler(shown)}
    require(i_d is not None and rec[i_d - 1] == "d"
            and rec[i_d] == f"D:1:{len(shown.encode('utf-8'))}:{adler(shown)}",
            "Show details didn't reveal the stock text", **ev)
    # screenshots: 2nd = friendly error up, 3rd = details open, both with the error marker (blue)
    shots = s["screens"]
    require(len(shots) >= 3 and shots[1]["overlay_square"] == "blue" and shots[2]["overlay_square"] == "blue",
            "the screenshots don't show the friendly error on screen", **ev)
    # Try again -> a new login -> character select
    i_r = tok(rec, "R", i_d)
    require(i_r is not None and rec[i_r - 1] == "r1", "Try again wasn't pressed on the friendly error", **ev)
    require(rec[i_r + 1:i_r + 2] and rec[i_r + 1].startswith("O:retry:"), "Try again didn't log in again", **ev)
    ph2 = _overlay_phase(rec, i_r + 1)
    ev["retry_phase"] = ph2
    lists = [t for t in ph2["tokens"] + rec[ph2["end"] + 1:ph2["end"] + 2] if re.fullmatch(r"L\d+", t)]
    require(ph2["hide"].get("why") == "charlist" and lists and int(lists[-1][1:]) >= 1,
            "Try again didn't reach character select", **ev)
    require(re.search(r"COP_GET_CHARACTERS code=\d+ result=TRUE", s["connection_log"] or ""),
            "the client's connection.log shows no character list", **ev)
    require(ev["record"]["lua_errors"] == "0" and all(x["lua_errors"] == 0 for x in shots),
            "Lua errors (test error handler)", **ev)
    require(not any((v or "").strip() for v in s["frame_log"].values()), "GlueXML.log / FrameXML.log hold errors", **ev)
    require(is_clear(s["remaining_after"]), "something is still running", **ev)
    return ev


# ---------------------------------------------------------------- login text (probe)

PROBE_STAY_S = 30            # spec: stays in the world for 30 s
FORBIDDEN_LOGIN = ("server", "realm", "azerothcore", "playerbot", "mod-")
# chat types that are other people talking openly (SharedDefines.h enum ChatMsg), not text addressed to the character
PUBLIC_CHAT = {0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x10, 0x11, 0x27, 0x29, 0x2C, 0x2D,
               0x30, 0x31, 0x33}
SELF_TEST_TEXT = "Moonglade self-test: server realm AzerothCore playerbot mod-test"


def _addressed(t: dict) -> bool:
    c = t.get("chat")
    return not (c and c["type"] in PUBLIC_CHAT)


def _hits(texts: list[dict]) -> list[dict]:
    out = []
    for t in texts:
        if not _addressed(t):
            continue
        for line in t.get("lines") or []:
            low = line.lower()
            words = [w for w in FORBIDDEN_LOGIN if w in low]
            if t["op"] == "SMSG_MOTD" or words:
                out.append({"op": t["op"], "line": line[:200], "words": words})
    return out


@check("client.login_text_clean", order=129, capability=CAP_CONN, stack="up", tier="full")
def login_text_clean(ctx):
    """A character logs in through the headless probe and stays in the world 30 s: no SMSG_MOTD and no system or chat
    message addressed to it containing server, realm, AzerothCore, playerbot or mod- (case-insensitive). Then the check
    shows it can see such a message: a GM announcement with those words (SOAP) reaches the same session and is
    flagged."""
    from .. import soap
    from ..probe import client as probe
    ev: dict = {"account": P.TEST_USER, "character": P.TEST_CHAR, "stay_s": PROBE_STAY_S}
    st = stack.status()
    ev["stack"] = {"processes": st["processes"], "soap_ready": st["soap_ready"]}
    require(all(st["processes"].values()) and st["soap_ready"], "GIVEN not met: the stack isn't running", **ev)
    _, ws, conn = probe.open_world(P.TEST_USER, P.TEST_PASS)
    with ws:
        chars = {c["name"].lower(): c for c in ws.char_enum()}
        ch = chars.get(P.TEST_CHAR.lower())
        require(ch is not None, "GIVEN not met: the test character doesn't exist", chars=sorted(chars), **ev)
        ev["login"] = ws.player_login(ch["guid"])
        t_in = time.time()
        ws.start_pump()
        while time.time() - t_in < PROBE_STAY_S:
            if ws.pump_error:
                break
            time.sleep(0.5)
        ev["pump_error"] = ws.pump_error
        texts = list(ws.texts)
        ev["texts_received"] = [{k: v for k, v in t.items() if k != "chat"} | ({"type": t["chat"]["type_name"]}
                                if t.get("chat") else {}) for t in texts][:60]
        ev["texts_count"] = len(texts)
        ev["opcode_trail_sample"] = [x[1] for x in ws.log if x[0] == "S>C"][:40]
        hits = _hits(texts)
        ev["hits"] = hits
        require(not ws.pump_error, "the probe session was lost", **ev)
        require(not hits, "the character received server-identity text at login", **ev)
        # the check can see such a message: send one as a server message (`announce`) and one as a system chat line
        # (`nameannounce`, CHAT_MSG_SYSTEM like mod-playerbots' login lines) and watch the same session flag both
        n0 = len(ws.texts)
        ev["self_test_gm"] = [soap.gm(f"{cmd} {SELF_TEST_TEXT}").evidence() for cmd in ("announce", "nameannounce")]
        _wait(lambda: len(ws.texts) >= n0 + 2, 15, step=0.2)
        time.sleep(1.0)
        seen = _hits(ws.texts[n0:])
        ev["self_test_hits"] = seen
        flagged = {h["op"] for h in seen if SELF_TEST_TEXT.lower() in h["line"].lower()
                   and set(FORBIDDEN_LOGIN) <= set(h["words"])}
        require(flagged == {"SMSG_CHAT_SERVER_MESSAGE", "SMSG_MESSAGECHAT"},
                "the check couldn't see the messages it sent (it can't fail)", **ev)
        try:
            ev["logout"] = ws.logout_pumped()
        except Exception as e:  # noqa: BLE001  (logout is cleanup; the verdict is above)
            ev["logout_error"] = str(e)
    return ev
