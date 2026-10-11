Timebox: 3 h of builder time.

- [ ] 1.1 `MoongladeParty.lua` with the plan table, `/mg party` and `/mg party clear`, plus Lua unit tests (`test_addon_party`)
- [ ] 1.2 Config: explicit `AutoAvoidAoe` and `ApplyInstanceStrategies`, with a unit test of the generated config
- [ ] 1.3 AutoBalance chat filter (pure logic in `modules/mod-moonglade/src/logic/`, C++ unit test `chat_filter`), wired in mod-moonglade; "AutoBalance" added to `client.login_text_clean`'s words
- [ ] 1.4 `dng.party_preset` (integration): the probe sends the plan parsed from the Lua table; checks roles, levels and gear, then the tank-player variant, in one server session
- [ ] 1.5 `docs/companions-in-dungeons.md`
