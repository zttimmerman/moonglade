# Design: In-client login screen

## Play client
- `env/client-play/` = `cp -cR client/ …` (APFS clone), then `patches/client/NNNN-*.json` (binary) and `patches/client/data/` (sources for patch MPQs) in order. A build stamp records the base manifest hash, each patch hash and each output hash; a rebuild with identical inputs is a no-op.
- `client.inspect` keeps guarding `client/`. The game writes `WTF/`, `Cache/` and `Logs/` into the play client, not into `client/`.
- One-time settings carry-over: if `client/WTF` holds player settings and the play client has none, copy them (never the other way).

## Patch 0001: GlueXML signature
- From the spike: `XML_VerifyTocSignature` @0x008165e0 returns 0/1/2/3, and the caller's switch quits on 0/1/2 (@0x004da7f1, @0x004da7fe, …). The builder pins the smallest edit that makes the result path behave as 3 (OK). Candidates: force the comparison/jump at the switch, or make the function return 3. Choose the one with the fewest bytes and no side effects, and document why. Record the before/after bytes and hashes.

## Login flow in GlueXML
- `AccountLogin.lua`/`.xml` overrides via our patch MPQ (`patch-enUS-4.MPQ` or the next free slot; the spike showed patch-N priority order).
- Credentials: the launcher writes a small generated Lua file (e.g. `Interface\GlueXML\HSProfiles.lua`, loose file or tiny MPQ) into the play client at each start. It holds the profile names and logins. It's the same secret already stored in `env/player.json` (gitignored); never committed, never written into `client/`.
- Continue: on show, the menu preselects the last-used profile's Continue button (Enter or click calls the client's login function). Other profiles are listed with Play. No countdown.
- Backdrop: `AccountLogin:SetModel(...)` plus glue music by choice. The choice is persisted in the client's own saved settings (or our generated file).
- Keep the realm list skipped (single local realm) and the character screen unchanged.

## Testing approach
- Runtime evidence without Screen Recording: auto-login is observable server-side (authserver/worldserver logs, account `last_login`, character enum).
- GlueXML errors land in `Logs/GlueXML.log` / `FrameXML.log` in the play client; the checks assert they're absent.
- Visual layout and backdrop look are human-verify.

## Risks
- The patched exe might behave differently in untested places → stock mode one setting away; human-verify play.
- The first launch of a new exe path under Wine may re-trigger macOS prompts → note for Zach.
