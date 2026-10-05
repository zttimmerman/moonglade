# Proposal: Launcher fixes (Zach's play test, 2026-10-05) + F25/F26

## Why
Zach play-tested the polished launcher. Cmd-Tab, full screen, Dock Quit and the loading window work, but:
1. Clicking Hearthstone while the loading window is up shows "already running" instead of bringing the loading window forward.
2. Dock Quit closes the game at once, then the app lingers silently while the server stops. It looks frozen, and someone might force-quit mid-shutdown.
3. Full screen looks stretched (night elf wide): no `gxResolution` is set, so the client uses a 4:3 default on a 3024×1964 (~3:2) display.

Verifier follow-ups: F25 (menu-waits is proven only on a used play client) and F26 (the GUI path rule misses shared helpers).

## What Changes
1. **Reopen during loading** brings the loading window forward (no "already running" dialog). Reopen always brings forward whatever Hearthstone is showing.
2. **Shutdown window:** after Dock Quit (or the game exiting), the launcher shows a small window in the loading-window style, "Saving and shutting down the world… please don't force-quit", until the stack is down, then exits. Errors during shutdown are shown, not hidden.
3. **Dynamic resolution default.**
   - At each launch the launcher reads the display (size, Retina scale) and, **only if the player hasn't chosen a resolution**, writes a `gxResolution` matching the display's aspect ratio.
   - If the launcher chose it and the display changes, it re-picks. A player's own choice (e.g. via in-game Video options) is never overwritten.
   - **The default between native Retina and half-size is decided by measurement** (CPU %, GPU busy %, frame time if readable) at both resolutions, recorded in design.md. Native is the default if it costs little; otherwise half-size.
4. **F25:** a fresh, never-used play client shows no login before Continue.
5. **F26:** the GUI path rule also covers the shared helpers the GUI session depends on.

## Capabilities
### Modified Capabilities
- `mac-app` (reopen during loading, shutdown window), `client-connectivity` (resolution default), `login-screen` (fresh-client menu waits), `harness-reporting` (GUI path rule).

## Spec-change log
(none yet)
