"""The shared-server fixture (openspec/changes/test-pyramid design 6; harness-control "Integration tests share one
server start" and "Reset between shared-server tests is checked, cheap and fail-closed").

One `hs up` serves every shared-server test (stack="up" integration checks) of a run; tests needing another config
profile run together after the default-profile ones (runner.segment orders them; runner._ensure_stack restarts the
worldserver once per profile). Around each shared-server test the fixture:
- hands character-scoped tests their own identity (a fresh account and two characters, created in bulk at run start)
  in ctx["identity"], and runs them two at a time;
- after the test, runs its registered undos (ctx["undo"]), then checks the shared state (one SOAP/SQL round:
  sharedfix.shared_state); a difference fails the test that ran last ("leaked state", with the difference as
  evidence) and the worldserver restarts before the next test;
- at the end of the run removes every identity it created.
"""
from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, stack
from .util import log

POOL_WORKERS = 2
IDENTITY_WORKERS = 16


class SharedServer:
    def __init__(self, checks: list):
        self.char_checks = [c for c in checks if c.scope == "character" and c.layer == "integration"]
        self.base = None
        self.identities: dict = {}
        self.resets: list[dict] = []
        self.leaks: list[dict] = []
        self.restarts: list[dict] = []
        self.fixtures: dict | None = None
        self.started = None

    # -- run start (the first shared-server test): bulk identities and world facts, in parallel
    def _start(self, ctx):
        from . import sharedfix, companion as C
        t0 = time.time()
        self.started = t0
        need = len(self.char_checks)
        rec: dict = {"identities": need}
        with ThreadPoolExecutor(2) as ex:
            fb = ex.submit(sharedfix.bulk_identities, need, IDENTITY_WORKERS) if need else None
            fw = ex.submit(C.world_facts_warm)
            if fb is not None:
                b = fb.result()
                ids = b["identities"]
                for c, i in zip(self.char_checks, ids):
                    self.identities[c.id] = i
                rec["identities_s"] = b["wall_s"]
            rec["world_facts"] = fw.result()
        rec["wall_s"] = round(time.time() - t0, 2)
        self.fixtures = rec
        self.base = sharedfix.shared_state()
        log(f"fixture: {need} identities, world facts {rec['world_facts'].get('from')} in {rec['wall_s']} s")

    def _undo(self, ctx: dict) -> list:
        out = []
        for fn in reversed(ctx.pop("undo", []) or []):
            try:
                fn()
                out.append({"undo": getattr(fn, "__name__", "?"), "ok": True})
            except Exception as e:  # noqa: BLE001
                out.append({"undo": getattr(fn, "__name__", "?"), "ok": False, "error": f"{type(e).__name__}: {e}"})
        return out

    def _reset(self, recs: list[dict], profile: str | None) -> None:
        """The reset between shared-server tests: compare with the state at the start of the shared segment."""
        from . import sharedfix
        own = {n for i in self.identities.values() for n in (i.char, i.alt)}
        r = sharedfix.shared_state(own)
        want = dict(self.base["state"])
        got = dict(r["state"])
        if profile and profile != config.DEFAULT_PROFILE:
            want["profile"] = profile            # a profile group's tests run under their own profile
        diff = {k: {"expected": want.get(k), "actual": got.get(k)} for k in set(want) | set(got)
                if want.get(k) != got.get(k)}
        self.resets.append({"after": [x["id"] for x in recs], "s": r["duration_s"], "leak": bool(diff)})
        if diff:
            words = describe(diff)
            self.leaks.append({"after": [x["id"] for x in recs], "diff": diff})
            for x in recs:
                if x["status"] == "pass":
                    x["status"] = "fail"
                    x["message"] = f"leaked state: {'; '.join(words)}"
                x.setdefault("evidence", {})["leaked_state"] = diff
            t0 = time.time()
            try:
                info = stack.restart_world(profile or config.DEFAULT_PROFILE)
            except Exception as e:  # noqa: BLE001
                info = {"error": f"{type(e).__name__}: {e}"}
            self.restarts.append({"after": [x["id"] for x in recs], "s": round(time.time() - t0, 1), "info": info})
            log(f"fixture: leaked state after {[x['id'] for x in recs]} ({'; '.join(words)}); worldserver restarted")
            self.base = sharedfix.shared_state()

    def run(self, batch: list, ctx: dict, ensure, say) -> list[dict]:
        from .runner import run_one
        t0 = time.time()
        ensure("up", ctx, batch[0].profile)
        if self.base is None:
            self._start(ctx)
        if time.time() - t0 > 0.5:      # the stack start / profile restart and the run-start fixtures: integration time
            ctx.setdefault("exec_log", []).append({"id": "(fixture)", "start": t0, "end": time.time(), "lane": "a",
                                                   "layer": "integration"})

        def one(c):
            cx = ctx if c.scope != "character" else dict(ctx, identity=self.identities.get(c.id))
            if c.scope == "character":
                cx["undo"] = []
            rec = run_one(c, cx, lambda *_a: None)
            rec.setdefault("evidence", {})
            if c.scope == "character" and self.identities.get(c.id):
                rec["evidence"]["fixture_identity"] = {"account": self.identities[c.id].user,
                                                       "characters": [self.identities[c.id].char,
                                                                      self.identities[c.id].alt]}
            undone = self._undo(cx)
            if undone:
                rec["evidence"]["undo"] = undone
            return rec
        if len(batch) > 1:
            out = []
            for k in range(0, len(batch), POOL_WORKERS):
                part = batch[k:k + POOL_WORKERS]
                with ThreadPoolExecutor(len(part)) as ex:
                    recs = list(ex.map(one, part))
                for r in recs:
                    r["pool"] = len(part) > 1
                self._reset(recs, batch[0].profile)
                out += recs
            return out
        recs = [one(batch[0])]
        self._reset(recs, batch[0].profile)
        return recs

    def finish(self, ctx: dict) -> dict:
        from . import sharedfix
        cleanup = None
        if self.identities:
            try:
                cleanup = sharedfix.cleanup_identities(list(self.identities.values()))
            except Exception as e:  # noqa: BLE001
                cleanup = {"error": f"{type(e).__name__}: {e}"}
        rs = [r["s"] for r in self.resets]
        return {"fixtures": self.fixtures, "resets": self.resets,
                "reset_median_s": round(statistics.median(rs), 3) if rs else None,
                "reset_max_s": max(rs) if rs else None, "leaks": self.leaks, "restarts": self.restarts,
                "identities": {k: {"account": v.user, "characters": [v.char, v.alt]} for k, v in self.identities.items()},
                "cleanup": cleanup}


def describe(diff: dict) -> list[str]:
    """Plain words for a leaked-state difference."""
    out = []
    for k, v in sorted(diff.items()):
        if k == "pause":
            out.append("world left paused" if (v["actual"] or ("",))[0] == "paused" else f"pause state changed: {v['actual']}")
        elif k == "saves":
            out.append("save interval changed" if "interval" in str(v["actual"]) or "interval" in str(v["expected"])
                       else "saves settings changed")
        elif k == "online":
            out.append(f"characters left online: {sorted(set(v['actual'] or []) - set(v['expected'] or []))}")
        elif k == "profile":
            out.append(f"config profile changed: {v['actual']}")
        else:
            out.append(f"{k} changed")
    return out
