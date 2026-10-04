# Proposal: In-client login screen (front menu step 2)

## Why
North star: the game's own login screen becomes the single-player front menu. Zach moved this ahead of M4 on 2026-10-04. Since the .app has no terminal, the PLAYER password is no longer shown anywhere, and typing it on every launch is frustrating. The launcher-side menu (step 1) is dropped.

ADR-0009 (accepted 2026-10-04) approves the mechanism: a derived **play client** built from the pristine `client/` plus a hash-checked patch queue, with one-step revert. It also approves the first binary patch, the GlueXML signature check.

## What Changes
1. **Play client:**
   - `harness/hs client-build` builds `env/client-play/` as an APFS clone of `client/`, then applies `patches/client/` in order. Binary patch records hold the offset, original bytes, new bytes, and the input/output SHA-256. Data patches are MPQs built from repo sources.
   - The build is deterministic, refuses a wrong base or a double apply, and never writes to `client/`.
   - A launcher setting `client = patched | stock` (default `patched` once this change passes) picks which client `./play` and the .app run.
   - The player's existing client settings (`WTF/`) are carried over into the play client once, so nothing is lost.
2. **Patch 0001 (binary):** the GlueXML signature result counts as OK, so a modified GlueXML loads instead of quitting. Exact bytes are pinned from analysis of a copy (ADR-0005) and recorded in an ADR-0009 appendix. Nothing else in Wow.exe changes.
3. **Our GlueXML (data patch MPQ):**
   - **Profiles:** the login screen lists this install's profiles (for now: the PLAYER account; the design allows more). The menu opens with a preselected **Continue** for the last-used profile (Enter or click, GlueXML's own login call); other profiles are listed below. No countdown.
   - **No password typing anywhere.** Profile credentials reach GlueXML through a file the launcher generates at start, inside the play client only, never committed.
   - **Backdrop picker:** WotLK / TBC / Vanilla (Dark Portal) login backdrop with matching music, remembered between launches. The spike showed all three models and tracks ship in the 3.3.5a MPQs.
   - Looks native: keeps the original animated backdrop, music and layout style.
4. **Out of scope (next change):** server-side options in the menu (world size, vanilla mode), which need the launcher and server to act on a choice before the world loads. Until then they stay in `env/settings.json`.

## Capabilities
### New Capabilities
- `play-client`: the derived, revertable patched client (ADR-0009).
- `login-screen`: profile list, auto-login, backdrop picker in the client's own login screen.

### Modified Capabilities
- `play-launcher` / `mac-app`: run the play client by default; `stock` stays one setting away (no scenario text changes; covered by new checks).

## Impact
New `hs client-build`, `patches/client/`, GlueXML sources under `client-mods/glue/` (built into a patch MPQ by `hs mpq write`), launcher wiring, new checks. Binary change limited to patch 0001 (ADR-0009). No server changes.

## Data provenance
No game data values change. Patch bytes come from disassembly of a copy of build 12340 (SHA-256 aa63a575…), cited with addresses.

## Spec-change log
- 2026-10-04: auto-login countdown replaced by a preselected Continue button (Zach agreed after a UX discussion). Reason: a countdown races the player, hides the menu where options will live, and can log in before server options are chosen; one Enter press costs nothing. Scenario "Cancel shows the profile list" became "Menu waits for the player" (same check id `login.cancel_shows_list`); `login.auto_login` now triggers Continue once. Not a loosening: login still requires zero typed credentials; a zero-click skip-menu setting may come later, off by default.
