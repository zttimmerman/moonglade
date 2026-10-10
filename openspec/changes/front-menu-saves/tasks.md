Estimates are builder-agent wall-clock hours, including build and harness time on the M2 Pro (cap -j6, ccache). Total ≈ 20 h of builder time in three runs (A: 1–2, B: 3–4, C: 5–6), plus one verifier cycle of ≈ 1.5 h.

## 1. Prototype (run A; owns the stack)
- [x] 1.1 Measure Q1 (save list reaches the character screen: fixed-size MPQ slot, then loose file), Q2 (control request from the character screen without leaving it) and Q3 (in-game load with a minimal module: does the character list after `SMSG_LOGOUT_COMPLETE` show the restored level?) on the test account with a scratch play client. Write the results into design.md "Prototype results". Stop and report if Q3 fails. (2 h)

## 2. Server: mod-moonglade `MoongladeSaves` (run A)
- [ ] 2.1 Save set and classification table in code, from design.md section 1. `Moonglade.Saves.*` config (dir, intervals, keep counts, enable). `config.write_all` renders the per-lane dir. Refuse to enable when `CharacterDatabase.WorkerThreads` ≠ 1. (1.5 h)
- [ ] 2.2 Writer: `SaveToDB` → `AsyncCommitTransaction` → query holder → `character.json.gz` + manifest. Atomic `.tmp-` → rename, rotation, disk guard, refusals (combat, dead, falling, BG/arena, between maps, bots). (2.5 h)
- [ ] 2.3 Loader: checks, before-load save, one transaction with fresh item/pet/set IDs, corpse and short-lived item rows, cache update. In-game path with `LogoutPlayer(false)` + instant logout. `HS_SAVES_TEST` fail-after-N switch (test account only). (3 h)
- [ ] 2.4 Autosave triggers (rest, flight, map change, min interval). `MGSAVE` addon-message verbs and replies. `.moonglade save load|status|list` commands. Coordinate the `OnPlayerCanUseChat` parser with `true-pause` if it has merged. (1.5 h)

## 3. Launcher (run B)
- [ ] 3.1 `harness/hslib/charsaves.py`: read manifests, build `HS_SAVES` with a generation, refresh it in the chosen Q1 mechanism, map (generation, index) to (character, save), delete on request. `P.PLAYER_SAVES`. (1.5 h)
- [ ] 3.2 `savesctl.py`: the `L` verb with its action (SOAP load + status poll, one at a time), build-12340 refusal. `play.py` wiring. `clientcfg` heals a token `accountName`. (1.5 h)

## 4. Client (run B)
- [ ] 4.1 Glue: character-screen Saves button and panel, confirm dialog, notice, `L` request (with the Q2 fallback if needed), `GetCharacterListUpdate()` after a load, `accountName` save/restore in `HSControl_Send`, and test actions for the GUI checks. (2.5 h)
- [ ] 4.2 Add-on: Esc-menu Saves window (list, save with name, load, delete), `Bindings.xml` quicksave/quickload with defaults read from the client's default bindings, and refusal messages. (2 h)

## 5. Checks and docs (run C)
- [ ] 5.1 Server and probe checks: `saves.roundtrip`, `saves.scope`, `saves.item_ids_fresh`, `saves.table_coverage`, `saves.manual`, `saves.quick_slot`, `saves.autosave_triggers`, `saves.refused_in_combat`, `saves.bots_skip`, `saves.atomic`, `saves.inworld_load`, `saves.load_ordering`, `saves.undo`, `saves.load_atomic`, `saves.load_refusals`, `saves.inworld_list`, `saves.list_wording`, `saves.addon_static`, `saves.player_untouched`. (3 h)
- [ ] 5.2 Offline and GUI checks: `savesctl.load_verb`, `savesctl.build_refused`, `savesctl.account_name_kept`, `saves.live_charselect_load`, `saves.live_charselect_list` (one shared client session). (1.5 h)
- [ ] 5.3 Builder pre-verification checklist (hs-change-loop), each new check 3×, one counterexample each. design.md "As built". Player notes for the human-verify scenario. (1.5 h)
