# Design: seamless polish

## Overlay progress
- The client's login steps arrive as the stock status dialog texts: `OPEN_STATUS_DIALOG` / `UPDATE_STATUS_DIALOG`, with LOGIN_STATE_*, REALM_LIST_* and CHAR_LIST_* strings, which HSSeamless already intercepts.
- Map each known step to a target fraction, from the client's own GlueStrings (read with `hs mpq`, not from memory). Unknown texts advance by a small step.
- Each frame, the shown value eases toward a creep ceiling just below the next step's target, then jumps when that step arrives. `max()` keeps it from ever going down.
- `BeginPhase` resets to 0. `EndPhase("charlist")` sets it to full for the final frame before hiding.
- **Test hook:** the HS_TEST record has 255 characters at most. Per load, record a compact summary: the start value, the number of values, the number of decreases (must be 0), the number of step-driven advances, and the final value. Don't log every sample.

## Character list layout
- Measure the stock anchors in `CharacterSelect.xml`: the realm name, Change Realm, and the first `CharSelectCharacterButton`.
- When we hide the realm controls, re-anchor the first button (or the list container) upward by their measured offset.
- **Test hook:** record the first button's top and the panel's top (`GetTop()`) in the Q token, or a new compact token.

## As built
(filled in by the builder)
