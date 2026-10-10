"""front-menu-saves, on the real client (capabilities character-saves and saves-channel).

One shared real-client session (seamless_checks.py style): the real ./play and Wow.exe in test mode against a scratch
play client (env/check-play-saves/), the harness test account HSTEST and its character P.TEST_CHAR, never the
player's. Before it, the test character gets two saves over SOAP (an older one at one level, a newer one at another)
and then a third level. The GlueXML test hook (HS_TEST) drives the screens; nothing injects keys except one Shift
press after the client comes up (checks/clientinput.py: without hardware input the client's idle logout ends every
world session at once).

  title menu   two `ctl:S` requests, then Continue                         (savesctl.account_name_kept)
  characters   Saves -> the older save -> Load -> confirm, after LOAD_AT_S (the client's login-server connection is
               closed by then, design.md "Prototype results"); the list is fetched again; Play   (saves.live_charselect_load)
  world        the add-on's test hook ("saves"): a manual save with a name, a quicksave, then back to the menu
  characters   back from the world (charselect_return): Saves opened; Play
  world        the hook's second visit quits the game                       (saves.live_charselect_list)

Evidence: the glue's compact test records (HSSeamless.lua tokens, handed out through WTF/Config.wtf at each world
entry, so the harness reads Config.wtf while the run goes on), the add-on's SavedVariables report, the launcher's
savesctl log and play-last.json, Players.log, the database, and pids."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .. import charsaves, db, paths as P, soap, stack
from ..registry import check, require

LOAD_AT_S = 40                   # character screen: the load is confirmed this long after the list is up
PLAY_AT_S = LOAD_AT_S + 25
GLUE = {"marker": True, "rec_skip": {"G": True, "H": True, "I": True, "O": True, "S": True},
        "script": [{"t": 4, "act": "shot"}, {"t": 6, "act": "ctl:S"}, {"t": 12, "act": "ctl:S"},
                   {"t": 18, "act": "continue"}],
        "charselect": [{"t": 2, "act": "shot"}, {"t": LOAD_AT_S - 3, "act": "saves:open"},
                       {"t": LOAD_AT_S - 2, "act": "saves:pick:2"}, {"t": LOAD_AT_S - 1, "act": "saves:load"},
                       {"t": LOAD_AT_S, "act": "saves:confirm"}, {"t": LOAD_AT_S + 8, "act": "showlog"},
                       {"t": LOAD_AT_S + 9, "act": "shot"}, {"t": PLAY_AT_S - 3, "act": "showlog"},
                       {"t": PLAY_AT_S - 2, "act": "shot"},
                       {"t": PLAY_AT_S, "act": "play"}],
        "charselect_return": [{"t": 3, "act": "saves:open"}, {"t": 4, "act": "showlog"}, {"t": 5, "act": "shot"},
                              {"t": 8, "act": "play"}]}
HOOK_NAME = "GUI test save"
SESSION_TIMEOUT = 420
_S: dict = {}


def _q(sql):
    return db.query(sql, "characters")


def _seed(guid: int) -> dict:
    """Two saves of the test character at different levels (older first), then a third level; the store is the test
    account's own (P.SAVES/HSTEST/<guid>)."""
    from .. import saves as S
    d = P.SAVES / P.TEST_USER / str(guid)
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    ev = {"cleared": str(d.relative_to(P.ROOT)) if P.ROOT in d.parents else str(d)}
    out = {}
    for label, level, tele in (("GUI older", 4, "Goldshire"), ("GUI newer", 5, "Stormwind")):
        soap.gm(f"character level {P.TEST_CHAR} {level}")
        soap.gm(f"tele name {P.TEST_CHAR} {tele}")
        before = set(x.name for x in d.iterdir()) if d.exists() else set()
        soap.gm(f".moonglade save take {guid} {label}")
        t = time.time()
        new = []
        while time.time() - t < 10 and not new:
            new = [x for x in (d.iterdir() if d.exists() else []) if x.name not in before and not x.name.startswith(".tmp")]
            time.sleep(0.2)
        out[label] = json.loads((new[0] / "manifest.json").read_text()) if new else None
        time.sleep(1.1)                      # distinct creation seconds: the newer one is listed first
    soap.gm(f"character level {P.TEST_CHAR} 7")
    pos = _q(f"SELECT CAST(position_x AS DOUBLE), CAST(position_y AS DOUBLE), map FROM characters WHERE guid={guid}")
    ev["seeded"] = {k: {"id": v["id"], "level": v["level"], "zone": v["zone"]} if v else None for k, v in out.items()}
    ev["level_before_run"] = 7
    ev["older_pos"] = None
    older = out.get("GUI older")
    if older:
        rows = json.loads(__import__("gzip").decompress((d / older["id"] / "character.json.gz").read_bytes()))
        cols = [c.split(" ")[0] for c in rows["schema"]["characters"]]
        r = rows["rows"]["characters"][0]
        ev["older_pos"] = {k: float(r[cols.index(c)]) for k, c in (("x", "position_x"), ("y", "position_y"))} | {
            "map": int(r[cols.index("map")])}
    return ev | {"manifests": out}


def session() -> dict:
    if "s" in _S:
        return _S["s"]
    from .. import fixtures, launch, play
    from .clientinput import press_shift
    from .playclient_util import PlayRun, build_scratch, connection_log, drop_scratch, fresh_scratch, screenshots
    from .play_checks import remaining, require_given_clear
    from .seamless_checks import addon_sv_path, config_value, glue_record, parse_saved_variables, server_logins
    ev: dict = {"account": P.TEST_USER, "glue_test": GLUE, "shared_run": "saves GUI (live_charselect_load + "
                "live_charselect_list + savesctl.account_name_kept)"}
    _S["s"] = ev
    scratch = run = None
    try:
        require_given_clear(ev)
        stack.up()
        fixtures.ensure()
        guid = int(db.scalar(f"SELECT guid FROM characters WHERE name='{P.TEST_CHAR}'", "characters"))
        ev["guid"] = guid
        ev |= _seed(guid)
        stack.down()
        scratch = fresh_scratch("saves")
        build_scratch(scratch)
        sv = addon_sv_path(scratch)
        sv.parent.mkdir(parents=True, exist_ok=True)
        sv.write_text('MoongladeTest = {\r\n\t["hook"] = "saves",\r\n\t["name"] = "%s",\r\n}\r\n' % HOOK_NAME)
        run = PlayRun("saves", glue_test=GLUE, scratch=scratch)
        run.start()
        up = run.wait_client()
        ev["client_up"] = {k: v for k, v in up.items() if k not in ("t", "wow")}
        if not up.get("up"):
            raise RuntimeError(f"the real Wow.exe never came up: {up.get('why')}")
        t_up = up["t"]
        wow0 = [w["pid"] if isinstance(w, dict) else w for w in launch.wow_processes()]
        t = time.time()
        while not screenshots(scratch) and time.time() - t < 90:
            time.sleep(0.5)
        ev["client_input"] = press_shift(run.tmp)
        records, logins, seen_rec = [], [], set()
        ws_pids = []
        t = time.time()
        cfg_seen = []
        while time.time() - t < SESSION_TIMEOUT and run.proc.poll() is None:
            al = config_value(scratch, "accountList")
            if not cfg_seen or cfg_seen[-1][1] != al:
                cfg_seen.append((round(time.time() - t_up, 1), al))
            rec = glue_record(scratch)
            if rec and ";".join(rec) not in seen_rec:
                seen_rec.add(";".join(rec))
                records.append({"t": round(time.time() - t_up, 1), "rec": rec, "wow": launch.wow_processes() and
                                [w["pid"] if isinstance(w, dict) else w for w in launch.wow_processes()],
                                "worldserver": stack.stack_processes()["worldserver"]})
            wsp = stack.stack_processes()["worldserver"]
            if wsp:                                   # (empty once the game has quit and the stack is stopping)
                ws_pids.append(str(wsp))
            time.sleep(0.5)
        ev["records"] = records
        ev["account_list_seen"] = cfg_seen
        ev["worldserver_pids_seen"] = sorted(set(ws_pids))
        ev["wow_pid_at_start"] = wow0
        ev["play_pid"] = run.proc.pid
        ev["account_name_cfg"] = config_value(scratch, "accountName")
        wtf_hits = []
        for f in (scratch / "WTF").rglob("*"):
            rel = str(f.relative_to(scratch))
            if re.search(r"HS[0-9A-F]{6}[SL]", rel.upper()):        # a folder or file named after a token
                wtf_hits.append(rel)
            elif f.is_file() and re.search(rb"HS[0-9A-F]{6}[SL]", f.read_bytes().upper()):
                wtf_hits.append(rel)
        ev["wtf_token_files"] = wtf_hits
        try:
            sv_data = parse_saved_variables(sv.read_text(errors="replace")).get("MoongladeTest") or {}
        except Exception as e:  # noqa: BLE001
            sv_data = {"parse_error": str(e)}
        ev["addon"] = {"saves": sv_data.get("saves"), "again": sv_data.get("again")} if isinstance(sv_data, dict) else sv_data
        try:
            ev["addon_settings"] = parse_saved_variables(sv.read_text(errors="replace")).get("MoongladeSettings")
        except Exception:  # noqa: BLE001
            ev["addon_settings"] = None
        shots = [p for p in screenshots(scratch) if p.stat().st_mtime >= t_up - 5]
        ev["shots"] = [p.name for p in shots]
        bind_files = sorted((scratch / "WTF").rglob("bindings-cache.wtf"))
        ev["bindings_files"] = {str(f.relative_to(scratch)): [l for l in f.read_text(errors="replace").splitlines()
                                                              if "MOONGLADE" in l] for f in bind_files}
        keep = P.ROOT / "harness" / "out" / "saves-gui"           # git-ignored: the last run's screenshots for a look
        import shutil as _sh
        _sh.rmtree(keep, ignore_errors=True)
        keep.mkdir(parents=True)
        for p in shots:
            _sh.copy(p, keep / p.name)
        ev["connection_log_tail"] = connection_log(scratch)[-1500:]
        ev["stop"] = run.stop()
        rec_play = run.record()
        ev["play_record"] = {"outcome": rec_play.get("outcome"), "pid": rec_play.get("pid"),
                             "savesctl_events": (rec_play.get("savesctl") or {}).get("events"),
                             "charsaves": rec_play.get("charsaves")}
        log = P.LOGS / "savesctl.log"
        ev["listener_log"] = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
        ev["server_logins"] = [x for x in server_logins(P.TEST_CHAR) if x["t"] >= int(t_up)]
        ev["remaining_after"] = remaining()
        return ev
    except Exception as e:  # noqa: BLE001
        ev["session_error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        if run is not None:
            run.cleanup()
        drop_scratch(scratch)


def _live() -> dict:
    ev = session()
    require(not ev.get("session_error"), f"the shared live run failed: {ev.get('session_error')}", **ev)
    return ev


def _tokens(rec: list[str], prefix: str) -> list[str]:
    return [t for t in rec if t.startswith(prefix)]


def _adler(s: str) -> str:
    from .seamless_checks import adler
    return adler(s)


@check("saves.live_charselect_load", order=402, capability="character-saves", stack="down", tier="full")
def live_charselect_load(ctx):
    """The play client at the character screen in a test run (listener up, the character with two saves): the script
    opens Saves, picks the older save and confirms: within 10 s the character list shows the saved level; the notice
    names the loaded save; the Wow.exe, launcher and worldserver pids are the same as before; no stock connecting or
    error dialog was seen; Play enters the world at the saved position."""
    ev = _live()
    older = ev["manifests"].get("GUI older")
    require(older and ev["manifests"].get("GUI newer"), "GIVEN not met: the two saves weren't seeded", **ev)
    first = next((r for r in ev["records"] if any(t.startswith("J") for t in r["rec"])), None)
    ev["first_record"] = first
    require(first, "no load result in the glue's record", **ev)
    rec = first["rec"]
    v = _tokens(rec, "V")
    j = _tokens(rec, "J")
    u = _tokens(rec, "U")
    n = _tokens(rec, "N:")
    ev["tokens"] = {"V": v, "J": j, "U": u, "N": n}
    m = re.match(r"J([ARU]):(\d+):(\d+):(\d+)$", j[0]) if j else None
    require(v and v[0].startswith("V2:SS:"), "GIVEN not met: the panel didn't list the character's two saves", **ev)
    require(m and m.group(1) == "A", "the load wasn't accepted", **ev)
    ev["load_secs"] = int(m.group(3)) / 10
    um = re.match(r"U(\d+):(\d+)$", u[0]) if u else None
    ev["list_secs"] = int(um.group(2)) / 10 if um else None
    require(um and int(um.group(1)) == older["level"] and ev["list_secs"] <= 10,
            "the character list didn't show the saved level within 10 s", **ev)
    require(n and n[0] == "N:" + _adler(f'Loaded "{older["label"]}"'), "the notice doesn't name the loaded save", **ev)
    require(int(m.group(4)) == 0, "a stock connecting or error dialog was seen", **ev)
    wow = {tuple(r["wow"] or []) for r in ev["records"]} | {tuple(ev["wow_pid_at_start"])}
    require(len(ev["worldserver_pids_seen"]) == 1 and len(wow) == 1 and ev["play_record"]["pid"] == ev["play_pid"],
            "a pid changed (game, launcher or worldserver)", **ev)
    login = ev["server_logins"]
    require(login and f"Level: {older['level']}" in login[0]["line"], "Play didn't enter the world at the saved level", **ev)
    pos = ev["older_pos"]
    require(pos is not None, "GIVEN not met: the older save's position is unknown", **ev)
    gui_saves = sorted((P.SAVES / P.TEST_USER / str(ev["guid"])).iterdir())
    hook_manual = [json.loads((x / "manifest.json").read_text()) for x in gui_saves
                   if (x / "manifest.json").exists() and json.loads((x / "manifest.json").read_text())["label"] == HOOK_NAME]
    ev["hook_manual_save"] = hook_manual[0]["id"] if hook_manual else None
    require(hook_manual, "no in-world save to read the entry position from", **ev)
    rows = json.loads(__import__("gzip").decompress((gui_saves[[x.name for x in gui_saves].index(hook_manual[0]["id"])]
                                                     / "character.json.gz").read_bytes()))
    cols = [c.split(" ")[0] for c in rows["schema"]["characters"]]
    r = rows["rows"]["characters"][0]
    entry = {"x": float(r[cols.index("position_x")]), "y": float(r[cols.index("position_y")]), "map": int(r[cols.index("map")])}
    ev["entered_at"] = entry
    require(entry["map"] == pos["map"] and abs(entry["x"] - pos["x"]) <= 0.5 and abs(entry["y"] - pos["y"]) <= 0.5,
            "Play didn't enter the world at the saved position", **ev)
    return ev


@check("saves.live_charselect_list", order=403, capability="character-saves", stack="down", tier="full")
def live_charselect_list(ctx):
    """A test run that entered the world, made a manual save and a quicksave, and exited to the character screen: the
    script opens Saves for that character, and the panel lists both new saves at the top with their labels, without
    restarting the game. Along the way (the add-on's own record, both world visits): no Lua error, and the default
    keys F6/F7 bound, set at most once (the first visit handles them, the second finds them handled) and saved once
    (the account's bindings-cache.wtf has each line once, and no other account folder has them)."""
    ev = _live()
    sv = (ev["addon"] or {}).get("saves") or {}
    ev["addon_saves"] = sv
    replies = sv.get("replies") or {}
    if isinstance(replies, dict):
        replies = [replies[k] for k in sorted(replies)]
    require(any(str(r).startswith("OK\tSAVE") for r in replies) and any(str(r).startswith("OK\tQUICK") for r in replies),
            "GIVEN not met: the in-world manual save and quicksave didn't both succeed", **ev)
    again = (ev["addon"] or {}).get("again") or {}
    ev["lua_errors"] = {"first_visit": sv.get("errors"), "second_visit": again.get("errors"),
                        "messages": [sv.get("error_msgs"), again.get("error_msgs")]}
    require(sv.get("errors") == 0 and again.get("errors") == 0, "Lua errors in the add-on during the two world visits",
            **ev)
    binds = ev["bindings_files"].get(f"WTF/Account/{P.TEST_USER}/bindings-cache.wtf", [])
    others = {k: v for k, v in ev["bindings_files"].items() if v and k != f"WTF/Account/{P.TEST_USER}/bindings-cache.wtf"}
    ev["keys"] = {"other_binding_files": others, "first_visit": sv.get("keys"), "changed": sv.get("keys_changed"), "second_visit": again.get("keys"),
                  "bound": [sv.get("quicksave_key"), sv.get("quickload_key")], "files": binds,
                  "settings": ev.get("addon_settings")}
    # (keys_changed is 0 when the account already had them: the client keeps account bindings in the server's
    # account data, so an earlier run's F6/F7 come back; "set once" is the add-on's keysSet and one line per key)
    require(sv.get("keys") in (1, 2) and again.get("keys") == "already" and (ev.get("addon_settings") or {}).get("keysSet") == 1
            and [sv.get("quicksave_key"), sv.get("quickload_key")] == ["F6", "F7"]
            and binds.count("bind F6 MOONGLADE_QUICKSAVE") == 1 and binds.count("bind F7 MOONGLADE_QUICKLOAD") == 1
            and not others,
            "F6/F7 weren't set and saved exactly once", **ev)
    later = [r for r in ev["records"] if any(t.startswith("V") for t in r["rec"]) and not any(t.startswith("J") for t in r["rec"])]
    ev["return_record"] = later[-1] if later else None
    require(later, "no record from the character screen after the world", **ev)
    v = _tokens(later[-1]["rec"], "V")[0]
    m = re.match(r"V(\d+):(.)(.):([0-9a-f]{4}):([0-9a-f]{4})$", v)
    ev["panel"] = v
    store = charsaves.saves_of(P.TEST_USER, ev["guid"])
    quick = next((x for x in store if x["kind"] == "quick"), None)
    ev["store_top"] = [(x["kind"], x["label"]) for x in store[:3]]
    require(m and m.group(2) == "Q" and m.group(3) == "S", "the panel's first two rows aren't the new quicksave and "
            "manual save", **ev)
    require(quick and m.group(4) == _adler(quick["label"])[:4] and m.group(5) == _adler(HOOK_NAME)[:4],
            "the panel's labels aren't the new saves'", **ev)
    wow = {tuple(r["wow"] or []) for r in ev["records"]} | {tuple(ev["wow_pid_at_start"])}
    require(len(wow) == 1 and len(ev["worldserver_pids_seen"]) == 1, "the game or the server restarted", **ev)
    return ev
