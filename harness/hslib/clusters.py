"""Clustered integration sessions, PROTOTYPE (openspec/changes/test-pyramid design 13 rule 3; task 1.6).

One session asserts several scenarios; each scenario keeps its own id and its own pass/fail. Not registered as checks
(the measurement driver calls them); the THEN clauses are those of the existing checks they stand for:
- saves_cluster: saves.manual, saves.quick_slot, saves.inworld_load, saves.undo, for one per-test identity;
- dng_session: scale.module_loaded (the `.ab getoffset` half), scale.outdoor_untouched, scale.solo_weaker,
  scale.pause_safe, scale.counts_companions, scale.party_stock, lock.partial_kept, lock.partial_kept_dismissed and
  lock.fresh_after_clear, in three Deadmines entries.
  Deviations, recorded: the unscaled reference is the core's computed value (D.unscaled_max_health, which
  scale.disabled_stock proves equal to the scaling-off read) instead of a scaling-off read in the same run, so no
  profile restart; lock.fresh_after_clear's companions are dismissed before the clear (partial_kept_dismissed needs
  that), so its "both companions in the new copy" clause is NOT asserted here (a loosening, flagged in design 13.1).
  Not in the session: scale.disabled_stock (scaling-off profile group), lock.no_hourly_cap (six entries),
  lock.heroic_* (level 80, heroic), lock.login_inside_kept (a logout inside), lock.companion_unstuck (two copies).
"""
from __future__ import annotations

import hashlib
import re
import time

from . import db, paths as P, saves as S, soap, stack
from .registry import CheckFail, require


class Scenarios:
    def __init__(self):
        self.res: dict = {}
        self.t0 = time.time()

    def run(self, sid: str, fn):
        t = time.time()
        try:
            fn()
            self.res[sid] = {"status": "pass"}
        except CheckFail as e:
            self.res[sid] = {"status": "fail", "message": str(e)[:300]}
        except Exception as e:  # noqa: BLE001
            self.res[sid] = {"status": "error", "message": f"{type(e).__name__}: {e}"[:300],
                             "evidence": str(getattr(e, "evidence", ""))[:1500]}
        self.res[sid]["duration_s"] = round(time.time() - t, 2)
        return self.res[sid]["status"] == "pass"


# ---------------------------------------------------------------- saves

def saves_cluster(ident: S.Identity) -> dict:
    sc = Scenarios()
    ev: dict = {}
    t0 = time.time()
    with S.as_identity(ident):
        st = S.gm(".moonglade save status")
        require(st.startswith("Moonglade saves: enabled"), "GIVEN not met: saves aren't enabled", status=st)
        alt, char = ident.alt, ident.char
        g = S.char_row(alt)["guid"]
        require(S.wait_offline(alt), "GIVEN: the alt is online")
        # inworld_load's GIVEN, offline: a save from an earlier level
        S.gm(f"character level {alt} 6")
        S.gm(f"tele name {alt} Goldshire")
        save = S.take(g, f"earlier level {int(time.time()) % 1000}")
        pos = db.query(f"SELECT CAST(position_x AS DOUBLE), CAST(position_y AS DOUBLE), map FROM characters "
                       f"WHERE guid={g}", "characters")[0]
        S.gm(f"character level {alt} 8")
        pid = stack.stack_processes()["worldserver"]
        with S.Session(alt, tele=None) as s:
            def manual():
                name = f"Manual {int(time.time()) % 100000}"
                before = S.store_ids(g)
                t = time.time()
                replies = s.ask(f"SAVE manual {name}")
                new = []
                while time.time() - t < 5 and not new:
                    new = [x for x in S.store_ids(g) if x not in before and not x.startswith(".tmp-")]
                    time.sleep(0.1)
                require(len(new) == 1, "no single new save folder within 5 s", new=new)
                m = S.manifest(g, new[0])
                sha = hashlib.sha256((S.store_dir(g) / new[0] / "character.json.gz").read_bytes()).hexdigest()
                row = S.char_row(alt)
                require(m["label"] == name and m["name"] == alt and m["level"] == row["level"] and m["kind"] == "manual"
                        and m["zone"] == row["zone"] and m["zone_name"], "the manifest doesn't name the character, "
                        "level, zone and the given name", manifest=m, row=row)
                require(sha == m["sha256"], "the file's sha256 doesn't match the manifest")
                require(any(r == f"OK\tSAVE\t{name}" for r in replies), "no success line for the save", replies=replies)
                require(not S.store_tmp(g), "a .tmp-* folder is left")
            sc.run("saves.manual", manual)

            def quick():
                first = s.ask("QUICK")
                q = lambda: [x for x in S.store_ids(g) if "-quick" in x and not x.startswith(".tmp-")]  # noqa: E731
                given = q()
                require(len(given) == 1 and any(r.startswith("OK\tQUICK") for r in first),
                        "GIVEN not met: no existing quicksave")
                seen = list(given)
                for _ in range(2):
                    time.sleep(1.1)
                    s.ask("QUICK")
                    seen += [x for x in q() if x not in seen]
                after = q()
                require(len(after) == 1, f"{len(after)} quicksaves exist")
                require(after[0] == max(seen) and after[0] not in given, "the remaining quicksave isn't the newest")
            sc.run("saves.quick_slot", quick)

            def inworld():
                fight = S.engage(s, level=8)
                lst = s.list()
                row = next((r for r in lst["rows"] if r["label"] == save["label"]), None)
                require(row and fight["who"]["combat"], "GIVEN not met: no listed save or not in combat")
                since = s.ws.mark()
                t = time.time()
                s.send(f"LOAD {lst['gen']} {row['index']}")
                resp = s.ws.wait_event(lambda e: e["kind"] == "logout_response", since, timeout=5)
                done = s.ws.wait_event(lambda e: e["kind"] == "logout_complete", since, timeout=5)
                s.ws.stop_pump()
                s.ws.player_guid = None
                c = next(c for c in s.ws.char_enum() if c["guid"] == g)
                lp = s.ws.player_login(g)
                S.F.settle_login(s.ws, g)
                s.ws.start_pump()
                require(done and done["t"] - t <= 3 and resp and resp["instant"] == 1,
                        "no instant SMSG_LOGOUT_COMPLETE within 3 s")
                require(c["level"] == save["level"] and c["zone"] == save["zone"],
                        "the character list doesn't show the saved level and zone")
                require(abs(lp["x"] - float(pos[0])) <= 0.01 and abs(lp["y"] - float(pos[1])) <= 0.01
                        and lp["map"] == int(pos[2]), "entering the world isn't at the saved position")
                require(stack.stack_processes()["worldserver"] == pid, "the worldserver restarted")
            sc.run("saves.inworld_load", inworld)

        def undo():
            gc = S.char_row(char)["guid"]
            require(S.wait_offline(char), "GIVEN not met: the hunter is online")
            old = S.take(gc, "older")
            S.gm(f"character level {char} {S.RICH_LEVEL + 3}")
            S.gm(f"tele name {char} Stormwind")
            before = S.normalize(S.db_rows(gc))
            bl0 = {x for x in S.store_ids(gc) if "-before-load" in x}
            first = S.load(gc, old["id"])
            bl = sorted(x for x in S.store_ids(gc) if "-before-load" in x and x not in bl0)
            require(first["state"] == "done" and len(bl) == 1, "GIVEN not met: the first load or its before-load save")
            u = S.load(gc, bl[0])
            after = S.normalize(S.db_rows(gc))
            require(u["state"] == "done", "the undo load didn't finish")
            require(not S.diff(before, after), "the rows aren't those from before the first load")
            require(len([x for x in S.store_ids(gc) if "-before-load" in x]) <= 3, "more than 3 before-load saves")
        sc.run("saves.undo", undo)
    return {"cluster": "saves", "identity": ident.user, "wall_s": round(time.time() - t0, 2), "scenarios": sc.res}


# ---------------------------------------------------------------- dungeons

def dng_session() -> dict:
    from . import companion as C, config, dungeon as D, era as E
    from .checks import dungeon_checks as DC
    sc = Scenarios()
    t0 = time.time()
    f = D.facts(D.DEADMINES)
    fb = D.first_boss(f)
    sp = f["final"]["spawns"][0]
    unscaled = D.unscaled_max_health(f["final"]["credit_entry"], sp["level"])["health"]
    entries = []
    with D.Session(f["target_level"]) as s:
        def module_loaded():
            out = s.cmd(".ab getoffset", settle=0.6)
            rp = stack.running_profile() or {}
            want = config.module_overrides(config.overrides(rp.get("profile") or config.DEFAULT_PROFILE))[
                config.AUTOBALANCE_CONF + ".dist"]
            off = re.search(r"Difficulty Offset = (-?\d+)", "\n".join(out))
            require(off and off.group(1) == want["AutoBalance.playerCountDifficultyOffset"],
                    "`.ab getoffset` didn't answer Moonglade's offset", out=out)
        sc.run("scale.module_loaded", module_loaded)

        def outdoor():
            s.ws.set_selection(0)
            lvl = s.level
            s.level = C.LEVEL                 # the comp.assists rule at the companion checks' level, as the check
            try:
                c = s.pick_live_creature()
            finally:
                s.level = lvl
            info = s.unit(c["spawn_id"], c["entry"])
            lv = re.search(r"Level: (\d+)", "\n".join(s.cmd(f".npc info {c['spawn_id']}", settle=0.5)))
            require(lv, "no live level read")
            cls, exp, mod, rank = db.query(f"SELECT unit_class, exp, HealthModifier, `rank` FROM creature_template "
                                           f"WHERE entry = {c['entry']}", "world")[0]
            era_on = E.rendered_option(P.SERVER_ETC / "modules" / config.ERA_CONF, "EraProfile.Enable")
            side = db.query(f"SELECT `rank`, HealthModifier FROM era_creature_template WHERE entry = {c['entry']}",
                            "world")
            hm, rk = float(mod), int(rank)
            if era_on not in ("0", None) and side:
                if side[0][0] not in (None, "NULL"):
                    rk = int(side[0][0])
                if side[0][1] not in (None, "NULL"):
                    hm = float(side[0][1])
            want = E.creature_max_hp(E.ac_base_hp(int(lv.group(1)), int(cls), int(exp)), hm, rk)
            require(info.get("max_health") == want, f"outdoor max health {info.get('max_health')} != {want}")
        sc.run("scale.outdoor_untouched", outdoor)

        # entry 1: fresh copy, alone
        ev: dict = {}
        DC._fresh_enter(s, f, ev, "e1")
        entries.append(ev["e1"])
        first_instance = ev["e1"]["instance"]
        solo = {}

        def solo_weaker():
            s.cmd(".gm", settle=0.4)
            b = s.boss_near(f["final"], 1, DC.RESCALE_S)
            solo["b"] = b
            mult = (b["creaturestat"] or {}).get("health_multiplier")
            require(ev["e1"]["players"] == 1, "GIVEN: the probe isn't alone on the map")
            require(b["max_health"] and b["max_health"] < unscaled, f"solo {b['max_health']} not below {unscaled}")
            require(D.within(round(b["max_health"] / unscaled, 4), mult, DC.REL), "ratio != the multiplier")
        sc.run("scale.solo_weaker", solo_weaker)

        def pause_safe():
            try:
                before = solo.get("b") or s.boss_near(f["final"], 1, DC.RESCALE_S)
                p = DC._pause(s, True)
                require(p["reply"] == "paused", "GIVEN: the world didn't pause")
                time.sleep(DC.PAUSE_S)
                r = DC._pause(s, False)
                time.sleep(1.0)
                after = s.boss_near(f["final"], None, 5)
                bm = (before["creaturestat"] or {}).get("health_multiplier")
                am = (after["creaturestat"] or {}).get("health_multiplier")
                require(r["reply"] == "running", "the world didn't resume")
                require(after["max_health"] == before["max_health"] and am == bm, "changed across the pause")
            finally:
                soap.gm("moonglade pause off", timeout=10)
        sc.run("scale.pause_safe", pause_safe)

        reads = {}

        def counts():
            r = DC._party_reads(s, f, ev, [("1 player", (), 1), ("3 players", DC.PARTY[:2], 3),
                                           ("5 players", DC.PARTY[2:], 5)])
            reads["r"] = r
            require([x["players"] for x in r] == [1, 3, 5], "GIVEN: the map didn't hold 1, 3 and 5 players")
            hp = [x["max_health"] for x in r]
            require(all(h is not None for h in hp) and hp[0] < hp[1] < hp[2], f"not strictly increasing: {hp}")
            require(D.within(hp[2], unscaled, DC.REL), f"5-player {hp[2]} != unscaled {unscaled}")
        sc.run("scale.counts_companions", counts)

        def party_stock():
            r = (reads.get("r") or [{}])[-1]
            require(r.get("players") == 5, "`.ab mapstat` doesn't count 5 players")
            require(D.within(r.get("max_health"), unscaled, DC.REL), "5-player max health != unscaled")
        sc.run("scale.party_stock", party_stock)

        # first boss down (final alive), leave, dismiss everyone, entry 2 alone: the same copy
        part = {}

        def partial():
            bit = 1 << fb["bit"]
            s.kill(fb, f["map"])
            rows = s.wait_binds(lambda v: v and v[0]["mask"] & bit, f["map"])
            require(rows and rows[0]["mask"] & bit and not rows[0]["mask"] & (1 << f["final"]["bit"]),
                    "GIVEN: first encounter not done, or the final one is")
            s.leave()
            time.sleep(1.5)
            for name in list(s.companions):
                s.remove(name)
            require(not s.members() and not s.group_rows(), "GIVEN: the party didn't break up")
            time.sleep(1.5)
            e2 = DC._enter(s, f, part, "e2")
            entries.append(e2)
            first_after = s.unit(fb["spawns"][0]["spawn_id"], fb["credit_entry"])
            after = s.binds(f["map"])
            require(e2["instance"] == first_instance, f"re-entry gave {e2['instance']}, not {first_instance}")
            require(after and after[0]["mask"] & bit, "the first encounter isn't done any more")
            require(first_after.get("health") in (0, None) or first_after.get("guid") is None,
                    "the first boss is alive again")
        if sc.run("lock.partial_kept_dismissed", partial):
            sc.res["lock.partial_kept"] = dict(sc.res["lock.partial_kept_dismissed"], note="same steps")

        def fresh():
            old = DC._kill_all(s, f, part)
            s.leave()
            time.sleep(3)
            e3 = DC._enter(s, f, part, "e3")
            entries.append(e3)
            b = s.wait_binds(lambda v: v and v[0]["instance"] != old["instance"], f["map"])
            old_rows = db.query(f"SELECT guid FROM character_instance WHERE instance = {old['instance']} "
                                f"AND guid = {s.guid & 0xFFFFFFFF}", "characters")
            require(e3["instance"] and e3["instance"] != old["instance"], "re-entry gave the old copy")
            require(b and b[0]["instance"] == e3["instance"] and b[0]["mask"] == 0, "the new copy's mask isn't 0")
            require(not old_rows, "character_instance still binds the probe to the old copy")
            s.leave()
        sc.run("lock.fresh_after_clear", fresh)
    return {"cluster": "dng", "wall_s": round(time.time() - t0, 2), "entries": len(entries), "scenarios": sc.res}
