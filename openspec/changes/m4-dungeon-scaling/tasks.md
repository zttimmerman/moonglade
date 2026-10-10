Estimates are agent-hours of builder work, including the from-empty builds they trigger (~12–15 min each with ccache). The verifier cycle is separate (≤90 min per the hs-change-loop budget).

## 1. Prototype (riskiest assumption first) and ADR
- [x] 1.1 **Prototype, throwaway branch `spike/m4-dungeon`** (5–6 h). Stop and report if (a) or (b) fails.
  - (a) mod-autobalance `1d707cb6` builds and boots with our core pin and the three core patches (0001–0003), and the worldserver log is clean.
  - (b) The headless probe gets into The Deadmines by GM teleport with `.gm off`, adds 4 addclass companions (prot warrior, holy priest, 2 dps), sets their specs and kills the first boss. Record the time, deaths and `completedEncounters` bit.
  - (c) The same character alone as a **selfbot** (`.playerbots bot self`) moves and fights headless. Record whether it follows splines, uses abilities and kills the first boss. If not, try the passive-master `attack` fallback (design.md §5).
  - (d) `.ab mapstat` player count with 1/3/5 players, and boss max health at each.
  - (e) `OnPlayerCanEnterMap` unbind-before-enter: re-entry after `.die` on every boss gives a new instance id.
  - (f) Whether `.go xyz` into map 36 honours access requirements.
  - Write `docs/research/m4-dungeon-spike.md` with the numbers.
- [ ] 1.2 **ADR-0014 "Third-party gameplay module: mod-autobalance"** (0.5 h, orchestrator): choice vs SoloCraft, pin, licence notice, upstream search (design.md §1), fallback plan.

## 2. Server
- [ ] 2.1 **Pin and build integration** (3–4 h): fork.lock keys; `build.py` (clone, checkout, clean, link, `BUILD_PINS`, manifest); `bootstrap.py`; baseline; `tools/release` (THIRD_PARTY notice from the source header, source manifest, conf in payload, golden paths). `hs build --force-clean` green; `build.*`, `release.*`, `public.*` green.
- [ ] 2.2 **Config** (1 h): `autobalance` section in `config.py` with the design.md §2 keys and reasons; `MODULE_SECTIONS` and lane B union; `AccountInstancesPerHour = 1000` in worldserver overrides; profiles `scaling-off` and `lockouts-stock`.
- [ ] 2.3 **mod-moonglade `MoongladeDungeons`** (3 h): `OnPlayerCanEnterMap` fresh-after-clear (design.md §3), `IsBot()` skip confirmed for addclass and alt bots, `loginCheck` skip, config key `Moonglade.Dungeons.FreshAfterClear` (default 1) in `mod_moonglade.conf.dist`, a log line per reset.

## 3. Harness
- [ ] 3.1 **`harness/hslib/dungeon.py` driver** (3 h): listed-dungeon table by LFG id, every fact by rule (design.md §6, with the rules as strings in evidence); enter/party/boss_info/completed_mask/kill helpers; GM-mode-off assertion.
- [ ] 3.2 **Checks `scale.module_loaded`, `scale.release_notice`, `scale.solo_weaker`, `scale.party_stock`, `scale.counts_companions`, `scale.disabled_stock`, `scale.outdoor_untouched`, `scale.pause_safe`** (4 h), each ≥3× plus one counterexample (design.md §5).
- [ ] 3.3 **Checks `lock.no_hourly_cap`, `lock.fresh_after_clear`, `lock.partial_kept`, `lock.heroic_fresh_after_clear`, `lock.heroic_partial_kept`, `lock.login_inside_kept`** (3 h), each ≥3× plus one counterexample.
- [ ] 3.4 **Tuning + `scale.solo_final_boss` (slow)** (4–5 h): run the four listed final bosses solo; adjust the inflection settings only as far as needed; record every run (settings, boss health, deaths, duration) in `docs/research/m4-scaling-tuning.md`. Apply Zach's answers to proposal questions 1–5 if they've arrived; otherwise ship the defaults.

## 4. Docs
- [ ] 4.1 **design.md "As built"; player notes** (what scaling and fresh-after-clear do, how to turn each off) (1 h).

**Total builder estimate: ~25–29 agent-hours.** Plus one verifier cycle (~1.5 h) and a full run.
- [ ] 2.4 MoongladeDungeons: keep partial progress when the group breaks up, and bring stranded companions out of old copies (`lock.partial_kept_dismissed`, `lock.companion_unstuck`) (5–6 h)
