"""dungeon-scaling and dungeon-lockouts (openspec/changes/m4-dungeon-scaling): scale.* and lock.* checks.

Each check logs in the dungeon checks' own character (P.DUNGEON_CHAR on P.DUNGEON_USER, security 2; never PLAYER,
HSTEST or Hscomp) through the probe; driver and every dungeon fact by rule: hslib/dungeon.py (design.md §5-6). Every
check that needs a fresh copy first clears the probe's own binds of that map (`.instance unbind <map>`), so it doesn't
depend on what an earlier check left. The lockout checks give boss credit with `.damage <current health>` (`.die`
gives none; spec-change log 2026-10-10). Tiers: full (1-3 min each) except scale.solo_final_boss (slow) and
scale.release_notice (slow, order 18 with signing.*: right after the release bracket, on the packaging release.* did in this run)."""
import json
import math
import re
import shutil
import subprocess
import time

from .. import companion as C, config, db, dungeon as D, era as E, paths as P, soap, stack
from ..probe import proto as W
from ..registry import check, require

CAP_SCALE = "dungeon-scaling"
CAP_LOCK = "dungeon-lockouts"
PARTY = (("warrior", "prot pve"), ("priest", "holy pve"), ("mage", "frost pve"), ("rogue", "combat pve"))
REL = 0.01                 # spec: "to within 1%"
UNSTUCK_S = 30             # spec: within 30 s
UNSTUCK_NEAR_YD = 10       # spec: within 10 yd
PAUSE_S = 5                # spec (test-pyramid delta): paused for 5 s; values compare to the millisecond
RESCALE_S = 20             # bound on waiting for AutoBalance to rescale the boss once its grid is active (dungeon.boss_near)
SCALING_OFF = "scaling-off"


def _run(fn):
    try:
        return fn()
    except C.CompanionError as e:
        require(False, str(e), **e.evidence)
    except C.probe.ProbeError as e:
        require(False, f"probe: {e}", **e.info)


def _sev(s) -> dict:
    return {k: s.ev.get(k) for k in ("account", "character", "prepare", "login", "gm_off", "stale_group", "revive")}


def _enter(s, f, ev: dict, key: str) -> dict:
    e = s.enter(f)
    ev[key] = {k: e.get(k) for k in ("ok", "instance", "event")} | {"players": (e.get("mapstat") or {}).get("players")}
    require(e["ok"], f"{key}: the probe didn't arrive in map {f['map']}", **ev)
    return e


def _fresh_enter(s, f, ev: dict, key: str = "enter") -> dict:
    ev[f"{key}_unbind"] = s.unbind(f["map"])
    require(ev[f"{key}_unbind"]["ok"], "GIVEN: the probe's old binds of the map didn't go away", **ev)
    return _enter(s, f, ev, key)


# ================================================================================================ scaling

def _unscaled_ref(ctx: dict, f: dict) -> dict:
    """The final boss's max health read under the scaling-off profile in this run (scale.disabled_stock records it;
    if it hasn't run, the stack is switched to scaling-off for the read and back)."""
    ref = ctx.get("m4_unscaled", {}).get(f["lfg_id"])
    if ref:
        return ref
    back = (stack.running_profile() or {}).get("profile") or config.DEFAULT_PROFILE
    rec = {"switched": stack.ensure_profile(SCALING_OFF)}
    try:
        with D.Session(f["target_level"]) as s:
            ev = {}
            _fresh_enter(s, f, ev)
            b = s.boss(f["final"])
            rec |= {"boss": b, "mapstat": ev["enter"], "max_health": b["max_health"], "profile": SCALING_OFF}
            s.leave()
    finally:
        rec["back"] = stack.ensure_profile(back)
    ctx.setdefault("m4_unscaled", {})[f["lfg_id"]] = rec
    return rec


# Declared ownership (harness-reporting "Impact selection is fail-safe"): what the checks of this file cover.
# First cut generated from the product paths they assert on and the harness modules this file imports directly
# (test-pyramid design 13.1); maintained by hand from here on. Shared infrastructure falls to the smoke set.
COVERS = (
    "modules/mod-moonglade/src/MoongladeDungeons.cpp",
    "modules/mod-moonglade/src/logic/Dungeons*",
    "modules/mod-moonglade/conf/mod_moonglade.conf.dist#Moonglade.Dungeons",
    "fork.lock#autobalance_*",
    "harness/hslib/build.py",
    "harness/hslib/checks/dungeon_checks.py",
    "harness/hslib/checks/release_checks.py",
    "harness/hslib/companion.py",
    "harness/hslib/dungeon.py",
    "harness/hslib/era.py",
    "harness/hslib/pause.py",
)
# The targeted lock.* checks (test-pyramid design 16) cover only their own code: the dungeon module, its settings,
# mod-autobalance's pin, the dungeon driver and this file (not build.py, era.py, pause.py, companion.py or
# release_checks.py, which are other checks' code; full runs still run them).
LOCK_COVERS = (
    "modules/mod-moonglade/src/MoongladeDungeons.cpp",
    "modules/mod-moonglade/src/logic/Dungeons*",
    "modules/mod-moonglade/conf/mod_moonglade.conf.dist#Moonglade.Dungeons",
    "fork.lock#autobalance_*",
    "harness/hslib/checks/dungeon_checks.py",
    "harness/hslib/dungeon.py",
)


def module_loaded(ctx):
    """The core's modules/ holds the harness symlink to the mod-autobalance checkout at the fork.lock commit, this
    worldserver's log shows the module's config load, `.ab getoffset` typed by a probe answers, and every AutoBalance
    key Moonglade sets has its Moonglade value in the rendered config."""
    lock = P.fork_lock()
    link = P.CORE_SRC / "modules" / "mod-autobalance"
    head = subprocess.run(["git", "-C", str(P.AB_SRC), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    ev = {"link": str(link), "link_target": str(link.readlink()) if link.is_symlink() else None,
          "expected_target": str(P.AB_SRC), "head": head, "pinned": lock.get("autobalance_sha")}
    require(link.is_symlink() and link.readlink() == P.AB_SRC, "core modules/mod-autobalance isn't the harness link", **ev)
    require(head and head == lock.get("autobalance_sha"), "vendor/mod-autobalance HEAD != fork.lock autobalance_sha", **ev)
    # this worldserver's config load: Server.log is rewritten at each worldserver start (Appender.Server ..., w);
    # the module stamps its load time in microseconds
    rp = stack.running_profile() or {}
    pid = rp.get("pid")
    started = None
    if pid:
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True)
        started = time.mktime(time.strptime(r.stdout.strip(), "%a %b %d %H:%M:%S %Y")) if r.stdout.strip() else None
    text = (P.LOGS / "Server.log").read_text(errors="replace") if (P.LOGS / "Server.log").exists() else ""
    loads = re.findall(r"AutoBalance::OnBeforeConfigLoad: Config loaded\. Global config time set to \((\d+)\)", text)
    ev |= {"worldserver_pid": pid, "worldserver_started": started, "config_load_lines": loads[-3:]}
    require(loads, "no AutoBalance config-load line in this worldserver's Server.log", **ev)
    load_t = int(loads[-1]) / 1e6
    ev["config_load_time"] = load_t
    require(started is not None and load_t >= started - 2, "the AutoBalance config load isn't from the running "
            "worldserver (older than its start)", **ev)
    # every Moonglade key, as rendered (lane A renders module confs into server/etc/modules)
    conf = (P.SERVER_ETC / "modules" / config.AUTOBALANCE_CONF).read_text()
    want = config.module_overrides(config.overrides(rp.get("profile") or config.DEFAULT_PROFILE))[config.AUTOBALANCE_CONF + ".dist"]
    got = {k: (re.search(rf"^{re.escape(k)}\s*=\s*(.*?)\s*$", conf, re.M) or [None, None])[1] for k in want}
    ev["keys"] = {k: {"want": v, "rendered": got[k]} for k, v in want.items()}
    bad = [k for k, v in want.items() if got[k] != v]
    require(not bad, f"AutoBalance keys without their Moonglade value in the rendered conf: {bad}", **ev)
    # the module's chat output stays off (seamless-experience; client.login_text_clean): rendered conf and the AC_*
    # variables a lane-B / release worldserver gets
    env = config.module_env(config.overrides(rp.get("profile") or config.DEFAULT_PROFILE), P.SERVER_ETC / "modules")
    ev["chat_keys"] = {k: {"rendered": got.get(k), "ac_env": env.get(config.env_var_name(k))}
                       for k in config.AUTOBALANCE_CHAT_OFF}
    loud = [k for k, v in ev["chat_keys"].items() if v["rendered"] != "0" or v["ac_env"] != "0"]
    require(not loud, f"AutoBalance chat output not off: {loud}", **ev)
    with D.Session(D.facts(D.DEADMINES)["target_level"]) as s:
        out = s.cmd(".ab getoffset", settle=0.6)
        ev["getoffset"] = out
        ev["session"] = _sev(s)
    require(not any("does not exist" in l for l in out) and any("Difficulty Offset = " in l for l in out),
            "`.ab getoffset` didn't answer", **ev)
    off = re.search(r"Difficulty Offset = (-?\d+)", "\n".join(out))
    require(off and off.group(1) == want["AutoBalance.playerCountDifficultyOffset"],
            "the running playerCountDifficultyOffset isn't Moonglade's", **ev)
    return ev


@check("scale.disabled_stock", order=501, capability=CAP_SCALE, stack="up", layer="integration", profile=SCALING_OFF)
def disabled_stock(ctx):
    """With AutoBalance disabled (scaling-off), the probe alone in The Deadmines reads the final boss's max health,
    and it equals the health the core computes with no module (ceil(BaseHealth x HealthModifier) x the rank's
    Rate.Creature.*.HP) to within 1%."""
    f = D.facts(D.DEADMINES)
    ev = {"facts": {k: f[k] for k in ("lfg_id", "map", "target_level", "rules")}, "final": f["final"]}

    def go():
        with D.Session(f["target_level"]) as s:
            ev["session"] = _sev(s)
            _fresh_enter(s, f, ev)
            ev["mapstat"] = s.mapstat()
            ev["boss"] = s.boss(f["final"])
            s.leave()
    _run(go)
    sp = f["final"]["spawns"][0]
    ev["computed"] = D.unscaled_max_health(f["final"]["credit_entry"], sp["level"])
    mx = ev["boss"]["max_health"]
    require(ev["mapstat"]["disabled"], "`.ab mapstat` doesn't report AutoBalance disabled under scaling-off", **ev)
    require(D.within(mx, ev["computed"]["health"], REL), f"final boss max health {mx} != the core's unscaled "
            f"{ev['computed']['health']} (1%)", **ev)
    ctx.setdefault("m4_unscaled", {})[f["lfg_id"]] = {"max_health": mx, "boss": ev["boss"], "profile": SCALING_OFF,
                                                      "by": "scale.disabled_stock"}
    return ev


def solo_weaker(ctx):
    """Alone in a fresh Deadmines at its target level (GM mode off), the final boss's max health is below its
    unscaled max health (the same spawn read under scaling-off in this run), and the ratio matches the multiplier
    `.ab creaturestat` reports for it to within 1%."""
    f = D.facts(D.DEADMINES)
    ev = {"final": f["final"]["spawns"][0], "target_level": f["target_level"]}
    ref = _run(lambda: _unscaled_ref(ctx, f))
    ev["unscaled"] = {k: ref.get(k) for k in ("max_health", "profile", "by")}

    def go():
        with D.Session(f["target_level"]) as s:
            ev["session"] = _sev(s)
            _fresh_enter(s, f, ev)
            ev["gm"] = s.cmd(".gm", settle=0.4)
            ev["boss"] = s.boss(f["final"])
            s.leave()
    _run(go)
    mx, un = ev["boss"]["max_health"], ref.get("max_health")
    mult = (ev["boss"]["creaturestat"] or {}).get("health_multiplier")
    require(ev["enter"]["players"] == 1, "GIVEN: the probe isn't alone on the map", **ev)
    require(mx and un and mx < un, f"solo max health {mx} isn't below the unscaled {un}", **ev)
    ev["ratio"] = round(mx / un, 4)
    require(D.within(ev["ratio"], mult, REL), f"ratio {ev['ratio']} != `.ab creaturestat` multiplier {mult} (1%)", **ev)
    return ev


def _party_reads(s, f, ev, steps) -> list[dict]:
    """Read the final boss's max health after each step: (label, classes to add first, expected player count)."""
    reads = []
    for label, add, n in steps:
        for cls, spec in add:
            ev.setdefault("added", []).append(s.add(cls, spec))
        if add:
            g = s.gather(n)
            ev.setdefault("gather", []).append(g)
            require(g["ok"], f"GIVEN: {n} players never on the map ({label})", **ev)
        ms = s.mapstat()
        b = s.boss_near(f["final"], ms["players"], RESCALE_S)
        reads.append({"label": label, "players": ms["players"], "adjusted": ms["adjusted"], "max_health": b["max_health"],
                      "health_multiplier": (b["creaturestat"] or {}).get("health_multiplier"), "in_combat": b["in_combat"],
                      "boss_player_count": (b["creaturestat"] or {}).get("player_count"),
                      "rescale_wait_s": b["rescale_wait_s"]})
    return reads


def party_stock(ctx):
    """The probe and four addclass companions in a fresh Deadmines, out of combat: `.ab mapstat` counts 5 players and
    the final boss's max health equals its unscaled max health (scaling-off read, this run) to within 1%."""
    f = D.facts(D.DEADMINES)
    ref = _run(lambda: _unscaled_ref(ctx, f))
    ev = {"unscaled": ref.get("max_health")}

    def go():
        with D.Session(f["target_level"]) as s:
            ev["session"] = _sev(s)
            _fresh_enter(s, f, ev)
            ev["reads"] = _party_reads(s, f, ev, [("5 players", PARTY, 5)])
            s.leave()
    _run(go)
    r = ev["reads"][-1]
    require(r["players"] == 5, f"`.ab mapstat` counts {r['players']} players, not 5", **ev)
    require(D.within(r["max_health"], ref.get("max_health"), REL),
            f"5-player max health {r['max_health']} != unscaled {ref.get('max_health')} (1%)", **ev)
    return ev


def counts_companions(ctx):
    """In a fresh Deadmines the final boss's max health with 1, 3 and 5 players (probe + 0, 2, 4 companions added out of
    combat) strictly increases, and the 5-player value equals the unscaled one to within 1%."""
    f = D.facts(D.DEADMINES)
    ref = _run(lambda: _unscaled_ref(ctx, f))
    ev = {"unscaled": ref.get("max_health")}

    def go():
        with D.Session(f["target_level"]) as s:
            ev["session"] = _sev(s)
            _fresh_enter(s, f, ev)
            ev["reads"] = _party_reads(s, f, ev, [("1 player", (), 1), ("3 players", PARTY[:2], 3),
                                                  ("5 players", PARTY[2:], 5)])
            s.leave()
    _run(go)
    r = ev["reads"]
    require([x["players"] for x in r] == [1, 3, 5], "GIVEN: the map didn't hold 1, 3 and 5 players", **ev)
    hp = [x["max_health"] for x in r]
    require(all(h is not None for h in hp) and hp[0] < hp[1] < hp[2], f"max health doesn't strictly increase: {hp}", **ev)
    require(D.within(hp[2], ref.get("max_health"), REL), f"5-player {hp[2]} != unscaled {ref.get('max_health')}", **ev)
    return ev


OUTDOOR_RULE = ("the comp.assists creature rule (companion.CREATURE_RULE) at the companion checks' master level "
                "(companion.LEVEL) around their start point: the first live, unhurt candidate out of combat")


@check("scale.outdoor_untouched", order=505, capability=CAP_SCALE, stack="up", layer="integration")
def outdoor_untouched(ctx):
    """With scaling on (default profile), an outdoor creature picked by the comp.assists rule has exactly the max health
    the world DB gives it (and, with vanilla mode on, the era side table's rank and HealthModifier), as without the
    module."""
    ev = {"rule": OUTDOOR_RULE}

    def go():
        with D.Session(C.LEVEL) as s:
            ev["session"] = _sev(s)
            s.ws.set_selection(0)
            c = s.pick_live_creature()
            info = s.unit(c["spawn_id"], c["entry"])
            lv = re.search(r"Level: (\d+)", "\n".join(s.cmd(f".npc info {c['spawn_id']}", settle=0.5)))
            ev["creature"] = {k: c[k] for k in ("spawn_id", "entry", "name", "minlevel", "maxlevel")}
            ev["live"] = {"max_health": info.get("max_health"), "level": int(lv.group(1)) if lv else None,
                          "map": (s.gps() or {}).get("map")}
            ev["mapstat"] = s.mapstat()
    _run(go)
    entry, level = ev["creature"]["entry"], ev["live"]["level"]
    require(level, "no live level read for the creature", **ev)
    cls, exp, mod, rank = db.query(f"SELECT unit_class, exp, HealthModifier, `rank` FROM creature_template "
                                   f"WHERE entry = {entry}", "world")[0]
    era_on = E.rendered_option(P.SERVER_ETC / "modules" / config.ERA_CONF, "EraProfile.Enable")
    side = db.query(f"SELECT `rank`, HealthModifier FROM era_creature_template WHERE entry = {entry}", "world")
    hm, rk = float(mod), int(rank)
    if era_on not in ("0", None) and side:
        if side[0][0] not in (None, "NULL"):
            rk = int(side[0][0])
        if side[0][1] not in (None, "NULL"):
            hm = float(side[0][1])
    base = E.ac_base_hp(level, int(cls), int(exp))
    ev["expected"] = {"health": E.creature_max_hp(base, hm, rk), "base_hp": base, "health_modifier": hm, "rank": rk,
                      "vanilla_mode": era_on, "era_side_row": side[0] if side else None}
    require(ev["live"]["max_health"] == ev["expected"]["health"],
            f"outdoor max health {ev['live']['max_health']} != the world DB's {ev['expected']['health']}", **ev)
    return ev


def pause_safe(ctx):
    """The probe alone in The Deadmines with the final boss scaled for one: after a 20 s true pause (the add-on's
    MGPAUSE message) and resume, the boss's max health and `.ab creaturestat` multiplier are unchanged."""
    f = D.facts(D.DEADMINES)
    ev = {}

    def go():
        with D.Session(f["target_level"]) as s:
            ev["session"] = _sev(s)
            try:
                _fresh_enter(s, f, ev)
                ev["before"] = s.boss_near(f["final"], 1, RESCALE_S)
                ev["pause"] = _pause(s, True)
                require(ev["pause"]["reply"] == "paused", "GIVEN: the world didn't pause", **ev)
                time.sleep(PAUSE_S)
                ev["resume"] = _pause(s, False)
                time.sleep(1.0)
                # read beside the boss (its grid active, so any rescale the pause caused would show)
                ev["after"] = s.boss_near(f["final"], None, 5)
                s.leave()
            finally:
                ev["resume_at_exit"] = soap.gm("moonglade pause off", timeout=10).evidence()
    _run(go)
    b, a = ev["before"], ev["after"]
    bm, am = (b["creaturestat"] or {}).get("health_multiplier"), (a["creaturestat"] or {}).get("health_multiplier")
    require(b["max_health"] and bm and bm < 1, "GIVEN: the final boss isn't scaled down for one player", **ev)
    require(ev["resume"]["reply"] == "running", "the world didn't resume", **ev)
    require(a["max_health"] == b["max_health"] and am == bm,
            f"changed across the pause: max health {b['max_health']} -> {a['max_health']}, multiplier {bm} -> {am}", **ev)
    return ev


def _pause(s, on: bool, timeout: float = 10.0) -> dict:
    """The add-on's pause request (pause.py; an add-on whisper to the character's own name) and its answer."""
    from .. import pause as PZ
    since = s.ws.mark()
    s.ws.chat(f"{PZ.PREFIX}\t{'1' if on else '0'}", W.CHAT_MSG_WHISPER, to=P.DUNGEON_CHAR, lang=PZ.LANG_ADDON)
    e = s.ws.wait_event(lambda e: e["kind"] == "chat" and e["type"] == W.CHAT_MSG_WHISPER and e["lang"] == -1
                        and e["msg"].startswith(PZ.PREFIX + "\t")
                        and e["msg"].split("\t", 1)[1] in (("paused", "refused", "disabled") if on else ("running",)),
                        since, timeout)
    return {"reply": e["msg"].split("\t", 1)[1] if e else None, "t": e["t"] if e else None}


# ================================================================================================ lockouts

@check("lock.no_hourly_cap", order=510, capability=CAP_LOCK, stack="up", layer="integration", targeted=True,
       covers=LOCK_COVERS)
def no_hourly_cap(ctx):
    """Five fresh Deadmines copies entered, cleared from the probe's save and left; the sixth entry arrives in a sixth
    distinct copy (SMSG_NEW_WORLD for the map) and no SMSG_TRANSFER_ABORTED TOO_MANY_INSTANCES arrives."""
    f = D.facts(D.DEADMINES)
    ev = {"cap_rendered": E.rendered_option(P.LANE_ETC / "worldserver.conf", "AccountInstancesPerHour"), "entries": []}

    def go():
        with D.Session(f["target_level"]) as s:
            ev["session"] = _sev(s)
            mark = s.ws.mark()
            for k in range(6):
                u = s.unbind(f["map"])
                e = s.enter(f)
                ev["entries"].append({"n": k + 1, "unbind_ok": u["ok"], "ok": e["ok"], "instance": e.get("instance"),
                                      "event": e.get("event")})
                if not e["ok"]:
                    break
                s.leave()
            ev["aborts"] = [x for x in s.ws.events_since(mark) if x["kind"] == "transfer_aborted"]
    _run(go)
    ok5 = [x for x in ev["entries"][:5] if x["ok"]]
    require(len(ok5) == 5 and all(x["unbind_ok"] for x in ev["entries"][:5]),
            "GIVEN: five fresh copies weren't entered, cleared and left", **ev)
    too_many = [a for a in ev["aborts"] if a["reason"] == W.TRANSFER_ABORT_TOO_MANY_INSTANCES]
    require(not too_many, "TRANSFER_ABORT_TOO_MANY_INSTANCES arrived", **ev)
    six = ev["entries"][5] if len(ev["entries"]) == 6 else {}
    ids = [x["instance"] for x in ev["entries"]]
    require(six.get("ok") and len(set(ids)) == 6, f"the sixth entry didn't arrive in a sixth distinct copy ({ids})", **ev)
    return ev


def _kill_all(s, f, ev, key="kills"):
    """Every encounter with a static spawn (design.md §6 'required encounters'), killed with credit."""
    ev[key] = [s.kill(e, f["map"]) for e in f["encounters"] if e["spawns"]]
    want = sum(1 << e["bit"] for e in f["encounters"] if e["spawns"])
    rows = s.wait_binds(lambda v: v and (v[0]["mask"] & want) == want, f["map"])
    ev[f"{key}_binds"] = rows
    require(rows and (rows[0]["mask"] & want) == want, "GIVEN: the completion isn't saved in the instance's mask", **ev)
    return rows[0]


def fresh_after_clear(ctx):
    """The probe and two companions clear The Deadmines (every encounter, completion saved), leave, and the probe enters
    again: a new instance id with mask 0, both companions in that same new copy, and no character_instance row for the
    old copy for the probe or either companion."""
    f = D.facts(D.DEADMINES)
    ev = {}

    def go():
        with D.Session(f["target_level"]) as s:
            ev["session"] = _sev(s)
            a = _fresh_enter(s, f, ev)
            ev["party"] = [s.add(cls) for cls, _ in PARTY[:2]]
            ev["gather"] = s.gather(3)
            require(ev["gather"]["ok"], "GIVEN: the companions never arrived", **ev)
            old = _kill_all(s, f, ev)
            ev["old"] = old
            ev["leave"] = s.leave()
            time.sleep(3)
            b = _enter(s, f, ev, "reenter")
            ev["gather_after"] = s.gather(3)
            ev["mapstat_after"] = s.mapstat()
            ev["binds_after"] = s.wait_binds(lambda v: v and v[0]["instance"] != old["instance"], f["map"])
            guids = [s.guid] + [p["guid"] for p in ev["party"]]
            ev["old_rows"] = db.query(f"SELECT guid FROM character_instance WHERE instance = {old['instance']} AND guid IN "
                                      f"({','.join(str(g & 0xFFFFFFFF) for g in guids)})", "characters")
            ev["companion_maps"] = {p["name"]: soap.gm(f"pinfo {p['name']}").output[-160:] for p in ev["party"]}
            s.leave()
    _run(go)
    old, new = ev["old"]["instance"], ev["reenter"]["instance"]
    require(new and new != old, f"re-entry gave instance {new}, the old one was {old}", **ev)
    b = ev["binds_after"]
    require(b and b[0]["instance"] == new and b[0]["mask"] == 0, "the new copy's completed-encounter mask isn't 0", **ev)
    ms = ev["mapstat_after"]
    require(ms["instance"] == new and ms["players"] == 3, "the companions aren't in the probe's new copy", **ev)
    require(not ev["old_rows"], "character_instance still binds someone to the old copy", **ev)
    return ev


def partial_kept(ctx):
    """The probe in The Deadmines with its first encounter done (final not done) walks out (teleport to the start
    point) and enters again: the same instance id, and the first encounter is still done."""
    return _partial(ctx, D.DEADMINES, 0)


def _partial(ctx, lfg, difficulty, dismiss=False, level=None):
    f = D.facts(lfg)
    fb = D.first_boss(f)
    ev = {"first": fb["spawns"][0], "final_bit": f["final"]["bit"], "difficulty": difficulty}

    def go():
        with D.Session(level or f["target_level"]) as s:
            ev["session"] = _sev(s)
            try:
                if difficulty:
                    ev["unbind_outside"] = s.unbind(f["map"])
                    ev["difficulty_set"] = s.set_difficulty(difficulty)
                a = _fresh_enter(s, f, ev)
                if dismiss:
                    ev["party"] = [s.add(cls) for cls, _ in PARTY[:2]]
                    ev["gather"] = s.gather(3)
                    require(ev["gather"]["ok"], "GIVEN: the companions never arrived", **ev)
                ev["kill"] = s.kill(fb, f["map"])
                bit = 1 << fb["bit"]
                ev["binds_kill"] = s.wait_binds(lambda v: v and v[0]["mask"] & bit, f["map"])
                require(ev["binds_kill"] and ev["binds_kill"][0]["mask"] & bit
                        and not ev["binds_kill"][0]["mask"] & (1 << f["final"]["bit"]),
                        "GIVEN: first encounter not done, or the final one is", **ev)
                ev["leave"] = s.leave()
                if dismiss:
                    time.sleep(1.5)
                    ev["dismiss"] = [s.remove(p["name"]) for p in ev["party"]]
                    ev["group_after"] = s.group_rows()
                    require(not s.members() and not ev["group_after"], "GIVEN: the party didn't break up", **ev)
                    time.sleep(1.5)
                ev["binds_outside"] = s.binds(f["map"])
                b = _enter(s, f, ev, "reenter")
                ev["first_after"] = s.unit(fb["spawns"][0]["spawn_id"], fb["credit_entry"])
                ev["binds_after"] = s.binds(f["map"])
                s.leave()
            finally:
                if difficulty:
                    ev["difficulty_reset"] = s.set_difficulty(0)
    _run(go)
    a, b = ev["enter"]["instance"], ev["reenter"]["instance"]
    require(a and b == a, f"re-entry gave instance {b}, not the unfinished {a}", **ev)
    rows = ev["binds_after"]
    require(rows and rows[0]["mask"] & (1 << fb["bit"]), "the first encounter isn't done any more (mask)", **ev)
    require(ev["first_after"].get("health") in (0, None) or ev["first_after"].get("guid") is None,
            "the first boss is alive again", **ev)
    return ev


def partial_kept_dismissed(ctx):
    """The probe with two companions kills The Deadmines' first boss (final alive), leaves, dismisses every companion
    (the party breaks up) and enters again alone: the same instance id, and the first boss is still dead."""
    return _partial(ctx, D.DEADMINES, 0, dismiss=True)


@check("lock.heroic_fresh_after_clear", order=514, capability=CAP_LOCK, stack="up", layer="integration", targeted=True,
       covers=LOCK_COVERS)
def heroic_fresh_after_clear(ctx):
    """The probe at level 80 with heroic difficulty clears Utgarde Keep (heroic; the core's permanent heroic bind),
    leaves and enters again the same server day: a fresh heroic copy (new instance id, mask 0)."""
    f = D.facts(D.UTGARDE_KEEP_HEROIC)
    ev = {"facts": {k: f[k] for k in ("lfg_id", "map", "difficulty", "target_level")}}

    def go():
        with D.Session(f["target_level"]) as s:
            ev["session"] = _sev(s)
            try:
                ev["unbind_outside"] = s.unbind(f["map"])
                ev["difficulty_set"] = s.set_difficulty(f["difficulty"])
                a = _fresh_enter(s, f, ev)
                ev["mapstat"] = s.mapstat()
                old = _kill_all(s, f, ev)
                ev["old"] = old
                ev["leave"] = s.leave()
                ev["binds_outside"] = s.binds(f["map"])
                b = _enter(s, f, ev, "reenter")
                ev["binds_after"] = s.wait_binds(lambda v: v and v[0]["instance"] != old["instance"], f["map"], timeout=8)
                ev["mapstat_after"] = s.mapstat()
                s.leave()
            finally:
                ev["difficulty_reset"] = s.set_difficulty(0)
    _run(go)
    require("Heroic" in "\n".join(ev["mapstat"]["raw"]), "GIVEN: the copy isn't heroic", **ev)
    require(ev["old"]["permanent"] == 1 and ev["old"]["difficulty"] == f["difficulty"],
            "GIVEN: the save isn't a permanent heroic bind", **ev)
    old, new = ev["old"]["instance"], ev["reenter"]["instance"]
    require(new and new != old, f"re-entry gave instance {new}, the cleared heroic copy was {old}", **ev)
    b = ev["binds_after"]
    require(b and b[0]["instance"] == new and b[0]["mask"] == 0, "the new heroic copy's mask isn't 0", **ev)
    return ev


@check("lock.heroic_partial_kept", order=515, capability=CAP_LOCK, stack="up", layer="integration", targeted=True,
       covers=LOCK_COVERS)
def heroic_partial_kept(ctx):
    """The probe in Utgarde Keep (heroic) with the first encounter done and the final one not done leaves and enters
    again: the same instance id, first encounter still done."""
    return _partial(ctx, D.UTGARDE_KEEP_HEROIC, 1)


@check("lock.login_inside_kept", order=516, capability=CAP_LOCK, stack="up", layer="integration", targeted=True,
       covers=LOCK_COVERS)
def login_inside_kept(ctx):
    """The probe logged out inside a fully cleared Deadmines copy logs in again: it is in the same instance id, and its
    bind (instance, permanent flag, mask) is unchanged."""
    f = D.facts(D.DEADMINES)
    ev = {}

    def first():
        with D.Session(f["target_level"]) as s:
            ev["session1"] = _sev(s)
            _fresh_enter(s, f, ev)
            ev["cleared"] = _kill_all(s, f, ev)
            ev["bind_before"] = s.binds(f["map"])
            ev["pos_before"] = s.gps()

    def second():
        with D.Session(f["target_level"], tele=None) as s:
            ev["session2"] = _sev(s)
            ev["mapstat_after"] = s.mapstat()
            ev["gps_after"] = s.gps()
            ev["bind_after"] = s.binds(f["map"])
            s.leave()
    _run(first)
    ev["offline_row"] = D.char_row()
    _run(second)
    require(ev["offline_row"]["map"] == f["map"], "GIVEN: the probe didn't log out inside the dungeon", **ev)
    old = ev["bind_before"][0]["instance"]
    require(ev["gps_after"].get("map") == f["map"] and ev["mapstat_after"]["instance"] == old,
            f"logged in to {ev['mapstat_after']['instance']} (map {ev['gps_after'].get('map')}), not instance {old}", **ev)
    require(ev["bind_after"] == ev["bind_before"], "the bind changed across the login", **ev)
    return ev


@check("lock.companion_unstuck", order=517, capability=CAP_LOCK, stack="up", layer="integration", targeted=True,
       covers=LOCK_COVERS)
def companion_unstuck(ctx):
    """A pool companion that logged out inside an older Deadmines copy, brought into the probe's party in a fresh copy
    and summoned, is in the probe's instance within 30 s and within 10 yd of it."""
    f = D.facts(D.DEADMINES)
    ev = {}

    def go():
        with D.Session(f["target_level"]) as s:
            ev["session"] = _sev(s)
            a = _fresh_enter(s, f, ev, "old_copy")
            c = s.add("warrior")
            ev["companion"] = c
            ev["gather_old"] = s.gather(2)
            require(ev["gather_old"]["ok"], "GIVEN: the companion never arrived in the old copy", **ev)
            ev["remove_inside"] = s.remove(c["name"])
            s.companions.pop(c["name"], None)
            time.sleep(2)
            ev["offline"] = C.bot_info(c["guid"])
            # its saved position: characters.map + instance_id (its bind goes when it leaves the party offline,
            # INSTANCE_RESET_GROUP_LEAVE with no Player, but it logs back in at that position)
            ev["saved_position"] = db.query(f"SELECT map, instance_id FROM characters WHERE guid = {c['guid']}",
                                            "characters")
            require(ev["offline"].get("online") == 0 and ev["saved_position"]
                    and [int(x) for x in ev["saved_position"][0]] == [f["map"], a["instance"]],
                    "GIVEN: the companion isn't logged out inside the old copy", **ev)
            ev["leave"] = s.leave()
            b = _fresh_enter(s, f, ev, "new_copy")
            require(b["instance"] != a["instance"], "GIVEN: the probe isn't in a newer copy", **ev)
            t0 = time.time()
            ev["bring"] = s.cmd(f".playerbots bot add {c['name']}", settle=1.0)
            s.companions[c["name"]] = ev["offline"]
            samples = []
            ok = False
            while time.time() - t0 < UNSTUCK_S:
                s.ws.chat("summon", W.CHAT_MSG_WHISPER, to=c["name"])
                time.sleep(2)
                ms = s.mapstat()
                me, bot = s.gps(), s.gps(c["guid"])
                same = "x" in me and "x" in bot and (me["x"], me["y"]) == (bot["x"], bot["y"])
                d = round(C.dist(me, bot), 2) if "x" in me and "x" in bot and not same else None
                samples.append({"t": round(time.time() - t0, 1), "players": ms["players"], "instance": ms["instance"],
                                "bot_map": bot.get("map"), "distance_yd": d, "gps_identical": same})
                if ms["players"] == 2 and ms["instance"] == b["instance"] and bot.get("map") == f["map"] \
                        and d is not None and d <= UNSTUCK_NEAR_YD:
                    ok = True
                    break
            ev["samples"] = samples
            ev["ok_after_s"] = round(time.time() - t0, 1) if ok else None
            s.leave()
    _run(go)
    require(ev["ok_after_s"] is not None, f"the companion wasn't in the probe's copy within {UNSTUCK_NEAR_YD} yd in "
            f"{UNSTUCK_S} s", **ev)
    return ev


# ================================================================================================ slow tier

@check("scale.solo_final_boss", order=520, capability=CAP_SCALE, stack="up", layer="gate")
def solo_final_boss(ctx):
    """For each listed dungeon (normal): the probe at the dungeon's target level, auto-geared at the configured quality
    limit, GM mode off, alone with the playerbots self-AI, placed by GM teleport near the final boss in a fresh copy,
    beats the final boss within 10 minutes and at most 2 attempts; the final encounter's bit is set in the copy's
    mask, and the evidence holds the boss's scaled health, the deaths and the fight length."""
    ev = {"dungeons": {}}
    for lfg in D.LISTED:
        f = D.facts(lfg)
        rec = ev["dungeons"].setdefault(str(lfg), {"map": f["map"], "target_level": f["target_level"],
                                                   "final": f["final"]["credit_entry"]})

        def go():
            with D.Session(f["target_level"]) as s:
                rec["session"] = _sev(s)
                try:
                    rec.update(D.solo_final_boss(s, f))
                finally:
                    s.leave()
        try:
            _run(go)
        except Exception as e:      # noqa: BLE001  record and go on with the next dungeon; judged below
            rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    ev["summary"] = {k: {"killed": v.get("killed"), "attempts": len(v.get("attempts", [])), "deaths": v.get("deaths"),
                         "total_s": v.get("total_s"), "error": v.get("error"), "boss_scaled": v.get("boss_scaled"),
                         "fight_s": [x.get("killed_after_s") or x.get("probe_died_after_s") for x in v.get("attempts", [])]}
                     for k, v in ev["dungeons"].items()}
    # THEN: the evidence holds the boss's scaled health (and its multiplier), the deaths and the fight length, for
    # every listed dungeon; a missing read fails
    unread = [k for k, v in ev["summary"].items() if not (v["boss_scaled"] or {}).get("max_health")
              or not (v["boss_scaled"] or {}).get("health_multiplier") or v["deaths"] is None
              or not any(t is not None for t in v["fight_s"])]
    require(not unread, f"no scaled boss health / multiplier, deaths or fight length recorded for LFG {unread}", **ev)
    bad = [k for k, v in ev["dungeons"].items() if not v.get("killed") or len(v.get("attempts", [])) > D.SOLO_ATTEMPTS
           or (v.get("total_s") or 1e9) > D.SOLO_LIMIT_S]
    require(not bad, f"final boss not beaten alone (within 10 min, at most 2 attempts) in LFG {bad}", **ev)
    return ev


@check("scale.release_notice", order=18, capability=CAP_SCALE, stack="any", layer="gate")
def release_notice(ctx):
    """In the release built by tools/release (this run's packaging): THIRD_PARTY carries mod-autobalance with its
    pinned commit and the GPL-2.0-or-later header of src/AutoBalance.cpp, the source assets hold its tree at that
    commit (SHA256SUMS-checked), and the payload has the module's config file; a tampered copy fails."""
    from . import release_checks as R
    ev = R._need_payload(ctx)
    probs, pev = _notice_problems(R.OUT, R.PAYLOAD)
    ev |= {"notice": pev, "problems": probs}
    require(not probs, f"{len(probs)} mod-autobalance release problem(s)", **ev)
    d = P.OUT / f"scale-release-notice-{int(time.time())}"
    try:
        (d / "THIRD_PARTY" / "mod-autobalance").mkdir(parents=True)
        shutil.copytree(R.PAYLOAD / "THIRD_PARTY", d / "THIRD_PARTY", dirs_exist_ok=True)
        (d / "server" / "etc" / "modules").mkdir(parents=True)
        n = d / "THIRD_PARTY" / "mod-autobalance" / "NOTICE"
        n.write_text(n.read_text().replace("any later version", "no later version"))
        shutil.copy2(R.PAYLOAD / "VERSION", d / "VERSION")
        bad, _ = _notice_problems(R.OUT, d, sources=False)
        ev["self_test_tampered"] = bad[:4]
        require(any("header" in b for b in bad) and any("AutoBalance.conf" in b for b in bad),
                "self-test: a tampered notice and a missing module config weren't caught", **ev)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return ev


def _notice_problems(out_dir, payload, sources: bool = True) -> tuple[list[str], dict]:
    from . import release_checks as R
    from .. import build as B
    lock = P.fork_lock()
    sha = lock["autobalance_sha"]
    probs, ev = [], {}
    tree, tp = B.expected_patched_tree(P.AB_SRC, sha, [])
    if not tree:
        return tp, ev
    src = subprocess.run(["git", "-C", str(P.AB_SRC), "show", f"{tree}:src/AutoBalance.cpp"], capture_output=True,
                         text=True).stdout
    header = src[:src.find("*/") + 2] if src.startswith("/*") else ""
    try:
        index = json.loads((payload / "THIRD_PARTY" / "INDEX.json").read_text())
    except (OSError, ValueError) as e:
        return [f"THIRD_PARTY/INDEX.json unreadable: {e}"], ev
    e = index.get("mod-autobalance")
    ev["index_entry"] = e
    if not e:
        probs.append("no THIRD_PARTY entry for mod-autobalance")
    else:
        if sha not in (e.get("note") or ""):
            probs.append("the mod-autobalance entry doesn't name the pinned commit")
        texts = [(payload / "THIRD_PARTY" / f).read_text(errors="replace") for f in e.get("files", [])
                 if (payload / "THIRD_PARTY" / f).is_file()]
        ev["notice_head"] = texts[0][:400] if texts else None
        if not header or "any later version" not in header:
            probs.append("the pinned src/AutoBalance.cpp has no GPL-2.0-or-later header to quote")
        if not any(header and header in t and sha in t for t in texts):
            probs.append("no mod-autobalance notice quotes the pinned src/AutoBalance.cpp header with its commit")
    conf = payload / "server" / "etc" / "modules" / (config.AUTOBALANCE_CONF + ".dist")
    ev["conf"] = {"path": str(conf), "exists": conf.is_file()}
    if not conf.is_file():
        probs.append(f"the payload has no server/etc/modules/{config.AUTOBALANCE_CONF}.dist")
    try:
        ver = json.loads((payload / "VERSION").read_text())
    except (OSError, ValueError):
        ver = {}
    if ver.get("autobalance_sha") != sha:
        probs.append("VERSION doesn't name the pinned autobalance_sha")
    if sources:
        sp, sev = R.source_problems(out_dir, payload)
        ev["sources"] = {"expected": sev.get("expected_sources", {}).get("mod-autobalance"),
                         "tree": sev.get("trees", {}).get("mod-autobalance")}
        probs += [p for p in sp if "autobalance" in p or "SHA256SUMS" in p]
        if not ev["sources"]["tree"]:
            probs.append("the mod-autobalance source tarball wasn't compared with the pinned tree")
    return probs, ev


# ================================================================================================ the clustered sessions
# test-pyramid design 14: two sessions of <= 90 s on one dungeon probe login; the companions added in session 1 stay
# in the party for session 2 (the probe stays in the world between them, registered as lingering for the fixture's
# reset). Both run in the scaling-on profile group, after scale.disabled_stock's scaling-off read (their unscaled
# reference, ctx["m4_unscaled"]).

SESSION1 = ("scale.module_loaded", "scale.solo_weaker", "scale.pause_safe", "scale.counts_companions", "scale.party_stock")
SESSION2 = ("lock.partial_kept", "lock.fresh_after_clear", "lock.partial_kept_dismissed")
_DNG: dict = {}
SCALING_ON = "scaling-on"


def _scenario(res: dict, cid: str, fn) -> bool:
    from ..registry import CheckFail
    t0 = time.time()
    try:
        res[cid] = {"status": "pass", "evidence": fn() or {"ok": 1}}
    except CheckFail as e:
        res[cid] = {"status": "fail", "message": str(e)[:500], "evidence": e.evidence or {"failed": 1}}
    except Exception as e:  # noqa: BLE001
        res[cid] = {"status": "error", "message": f"{type(e).__name__}: {e}"[:500], "evidence": {"error": str(e)[:500]}}
    res[cid]["duration_s"] = round(time.time() - t0, 2)
    return res[cid]["status"] == "pass"


@check("dng.session1", order=502, capability=CAP_SCALE, stack="up", layer="integration", profile=SCALING_ON,
       budget_s=90, children=SESSION1)
def session1(ctx):
    """One dungeon probe login: scale.module_loaded, then in a fresh Deadmines alone: scale.solo_weaker and
    scale.pause_safe (a 5 s pause), then with 2 and 4 companions added: scale.counts_companions and scale.party_stock.
    The probe and its four companions stay in the instance for dng.session2."""
    f = D.facts(D.DEADMINES)
    res: dict = {}
    ev: dict = {}
    _scenario(res, "scale.module_loaded", lambda: module_loaded(ctx))
    ref = (ctx.get("m4_unscaled") or {}).get(f["lfg_id"]) or _run(lambda: _unscaled_ref(ctx, f))
    unscaled = ref.get("max_health")
    ev["unscaled"] = {k: ref.get(k) for k in ("max_health", "profile", "by")}
    s = D.Session(f["target_level"])
    _run(s.__enter__)
    _DNG.clear()
    _DNG.update({"s": s, "f": f, "unscaled": unscaled})
    ctx.setdefault("lingering", set()).add(P.DUNGEON_CHAR)
    try:
        ev["session"] = _sev(s)
        _fresh_enter(s, f, ev, "e1")
        _DNG["first_instance"] = ev["e1"]["instance"]
        solo = {}

        def solo_weaker():
            e = {"unscaled": ev["unscaled"], "enter": ev["e1"]}
            e["gm"] = s.cmd(".gm", settle=0.4)
            b = s.boss_near(f["final"], 1, RESCALE_S)
            solo["b"] = b
            e["boss"] = b
            mx, mult = b["max_health"], (b["creaturestat"] or {}).get("health_multiplier")
            require(ev["e1"]["players"] == 1, "GIVEN: the probe isn't alone on the map", **e)
            require(mx and unscaled and mx < unscaled, f"solo max health {mx} isn't below the unscaled {unscaled}", **e)
            e["ratio"] = round(mx / unscaled, 4)
            require(D.within(e["ratio"], mult, REL), f"ratio {e['ratio']} != `.ab creaturestat` multiplier {mult} (1%)", **e)
            return e
        _scenario(res, "scale.solo_weaker", solo_weaker)

        def pause_safe():
            e = {}
            try:
                e["before"] = solo.get("b") or s.boss_near(f["final"], 1, RESCALE_S)
                e["pause"] = _pause(s, True)
                require(e["pause"]["reply"] == "paused", "GIVEN: the world didn't pause", **e)
                time.sleep(PAUSE_S)
                e["resume"] = _pause(s, False)
                time.sleep(1.0)
                e["after"] = s.boss_near(f["final"], None, 5)
            finally:
                e["resume_at_exit"] = soap.gm("moonglade pause off", timeout=10).evidence()
            b, a = e["before"], e["after"]
            bm, am = (b["creaturestat"] or {}).get("health_multiplier"), (a["creaturestat"] or {}).get("health_multiplier")
            require(b["max_health"] and bm and bm < 1, "GIVEN: the final boss isn't scaled down for one player", **e)
            require(e["resume"]["reply"] == "running", "the world didn't resume", **e)
            require(a["max_health"] == b["max_health"] and am == bm,
                    f"changed across the pause: max health {b['max_health']} -> {a['max_health']}, multiplier {bm} -> {am}", **e)
            return e
        _scenario(res, "scale.pause_safe", pause_safe)
        reads = {}

        def counts():
            e = {"unscaled": unscaled}
            r = _party_reads(s, f, e, [("1 player", (), 1), ("3 players", PARTY[:2], 3), ("5 players", PARTY[2:], 5)])
            reads["r"] = r
            e["reads"] = r
            require([x["players"] for x in r] == [1, 3, 5], "GIVEN: the map didn't hold 1, 3 and 5 players", **e)
            hp = [x["max_health"] for x in r]
            require(all(h is not None for h in hp) and hp[0] < hp[1] < hp[2], f"max health doesn't strictly increase: {hp}", **e)
            require(D.within(hp[2], unscaled, REL), f"5-player {hp[2]} != unscaled {unscaled}", **e)
            return e
        _scenario(res, "scale.counts_companions", counts)

        def party():
            r = (reads.get("r") or [{}])[-1]
            e = {"unscaled": unscaled, "read": r}
            require(r.get("players") == 5, f"`.ab mapstat` counts {r.get('players')} players, not 5", **e)
            require(D.within(r.get("max_health"), unscaled, REL),
                    f"5-player max health {r.get('max_health')} != unscaled {unscaled} (1%)", **e)
            return e
        _scenario(res, "scale.party_stock", party)
    except Exception:
        _DNG.pop("s", None)
        try:
            s.__exit__(None, None, None)
        finally:
            ctx.get("lingering", set()).discard(P.DUNGEON_CHAR)
        raise
    ev["children"] = res
    return ev


@check("dng.session2", order=503, capability=CAP_LOCK, stack="up", layer="integration", profile=SCALING_ON,
       budget_s=90, children=SESSION2)
def session2(ctx):
    """Continues dng.session1's login, in its Deadmines copy A, with two of its four companions kept in the party:
    (lock.partial_kept) the first boss killed (final alive), the probe walks out and enters again: copy A, the first
    encounter still done; (lock.fresh_after_clear) the probe and the two companions clear A (every encounter, completion
    saved), leave, and the probe enters again: a new copy B with mask 0, both companions in B, and no bind to A for any
    of the three; (lock.partial_kept_dismissed) in B the first boss killed (final alive), the probe leaves, dismisses
    both companions (the party breaks up) and enters again alone: copy B, the first boss still dead."""
    res: dict = {}
    ev: dict = {}
    s, f = _DNG.get("s"), _DNG.get("f")
    if s is None:
        for cid in SESSION2:
            res[cid] = {"status": "error", "message": "GIVEN: dng.session1's login and companions aren't there",
                        "evidence": {"session1": "missing"}}
        return {"children": res}
    fb = D.first_boss(f)
    bit, final_bit = 1 << fb["bit"], 1 << f["final"]["bit"]
    st: dict = {}

    def first_kill(e: dict, key: str) -> None:
        e[key] = s.kill(fb, f["map"])
        e[f"{key}_binds"] = s.wait_binds(lambda v: v and v[0]["mask"] & bit, f["map"])
        rows = e[f"{key}_binds"]
        require(rows and rows[0]["mask"] & bit and not rows[0]["mask"] & final_bit,
                "GIVEN: first encounter not done, or the final one is", **e)

    def same_copy_after(e: dict, inst: int) -> None:
        b = _enter(s, f, e, "reenter")
        e["first_after"] = s.unit(fb["spawns"][0]["spawn_id"], fb["credit_entry"])
        e["binds_after"] = s.binds(f["map"])
        require(inst and b["instance"] == inst, f"re-entry gave instance {b['instance']}, not the unfinished {inst}", **e)
        require(e["binds_after"] and e["binds_after"][0]["mask"] & bit, "the first encounter isn't done any more (mask)", **e)
        require(e["first_after"].get("health") in (0, None) or e["first_after"].get("guid") is None,
                "the first boss is alive again", **e)
    try:
        # two companions stay (the lockout scenarios' party); the other two are dismissed
        keep = list(s.companions)[:2]
        ev["dismissed_extra"] = [s.remove(n) for n in list(s.companions)[2:]]
        ev["kept"] = keep
        st["party"] = [dict(s.companions[n], name=n) for n in keep]

        def partial():
            e = {"copy": _DNG["first_instance"], "companions": keep}
            require(len(keep) == 2, "GIVEN: the companions from dng.session1 aren't in the party", **e)
            first_kill(e, "kill")
            e["leave"] = s.leave()
            same_copy_after(e, _DNG["first_instance"])
            return e
        ok_partial = _scenario(res, "lock.partial_kept", partial)

        def fresh():
            e = {"companions": keep}
            require(ok_partial, "GIVEN: lock.partial_kept's steps didn't leave the probe in its copy", **e)
            e["gather"] = s.gather(3)
            require(e["gather"]["ok"], "GIVEN: the companions never arrived", **e)
            old = _kill_all(s, f, e)
            e["old"] = old
            e["leave"] = s.leave()
            time.sleep(3)
            new = _enter(s, f, e, "reenter")
            e["gather_after"] = s.gather(3)
            e["mapstat_after"] = s.mapstat()
            e["binds_after"] = s.wait_binds(lambda v: v and v[0]["instance"] != old["instance"], f["map"])
            guids = [s.guid] + [c["guid"] for c in st["party"]]
            e["old_rows"] = db.query(f"SELECT guid FROM character_instance WHERE instance = {old['instance']} AND guid IN "
                                     f"({','.join(str(g & 0xFFFFFFFF) for g in guids)})", "characters")
            require(new["instance"] and new["instance"] != old["instance"],
                    f"re-entry gave instance {new['instance']}, the old one was {old['instance']}", **e)
            b = e["binds_after"]
            require(b and b[0]["instance"] == new["instance"] and b[0]["mask"] == 0, "the new copy's completed-encounter mask isn't 0", **e)
            ms = e["mapstat_after"]
            require(ms["instance"] == new["instance"] and ms["players"] == 3, "the companions aren't in the probe's new copy", **e)
            require(not e["old_rows"], "character_instance still binds someone to the old copy", **e)
            st["copy_b"] = new["instance"]
            return e
        ok_fresh = _scenario(res, "lock.fresh_after_clear", fresh)

        def dismissed():
            e = {"copy": st.get("copy_b"), "companions": keep}
            require(ok_fresh and st.get("copy_b"), "GIVEN: lock.fresh_after_clear's steps didn't leave the probe with "
                    "its two companions in a fresh copy", **e)
            first_kill(e, "kill")
            e["leave"] = s.leave()
            time.sleep(1.5)
            e["dismiss"] = [s.remove(n) for n in list(s.companions)]
            e["group_after"] = s.group_rows()
            require(not s.members() and not e["group_after"], "GIVEN: the party didn't break up", **e)
            time.sleep(1.5)
            e["binds_outside"] = s.binds(f["map"])
            same_copy_after(e, st["copy_b"])
            return e
        _scenario(res, "lock.partial_kept_dismissed", dismissed)
        try:
            s.leave()
        except Exception:  # noqa: BLE001
            pass
    finally:
        _DNG.clear()
        try:
            s.__exit__(None, None, None)
        finally:
            ctx.get("lingering", set()).discard(P.DUNGEON_CHAR)
    ev["children"] = res
    return ev
