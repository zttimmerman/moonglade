# Seamless polish

## Why
Zach's play test of the seamless experience on 2026-10-07 turned up two issues:
1. **The loading overlay's bar looks like it goes backwards.** The "Entering Azeroth…" overlay's bar is a ping-pong sweep (`HSSeamless_OnUpdate`: width follows a triangle wave over `S.t * 0.6`). It isn't tied to login progress at all, so it fills and empties every ~1.7 s. It was most visible on a quick Play → Main Menu → Continue.
2. **A gap above the first character.** Hiding the realm name and the Change Realm button left their space empty above the first character in the character list (screenshot in the 2026-10-07 session).

## What changes
- **The overlay bar shows real progress.** It moves forward at each login step the client reports (the stock status dialog texts intercepted by `GlueDialog_Show` / `UPDATE_STATUS_DIALOG`), creeps toward the next step while waiting, never decreases within a load, and is full when the character list appears. Every new load starts from zero.
- **The character list starts at the top of its panel** when the realm controls are hidden, with the same spacing stock WoW uses between the panel's top edge and the first character.

## Impact
- Code: `client-mods/glue/HSSeamless.lua` (and its XML, if any); harness checks in `harness/hslib/checks/seamless_checks.py`.
- No binary patch change; it's the existing add-on under ADR-0009's GlueXML mechanism.
- Capability: login-screen.

## Spec-change log
(none yet)
