# ADR-0010: Wine host shim for one Hearthstone Dock icon

- Status: **accepted** (2026-10-04). Zach's yes ("let's do A") was conditional on no performance impact; that condition is enforced by a check (`app.shim_perf`). If it fails, the shim is off by default until fixed.
- Research: `docs/research/launcher-polish-spike.md` (branch `spike/launcher-polish`, 8846af5).

## Context
- When the game runs from `Hearthstone.app`, the Dock shows two icons: the launcher, and Wine's process for Wow.exe (labelled "wine", WoW's icon). Clicking Hearthstone says "already running"; only the Wine icon focuses the game (Zach's play test, 2026-10-04).
- Wine's Mac driver sets the Dock icon from the exe's icon resource and forces a regular Dock app on the first window. It has no registry key or env var to hide or rename it (Wine 11.13 `macdrv_main.c:279-385`, `cocoa_app.m:223-233,304`).
- ADR-0009 rejected "runtime memory patching or DLL injection under Wine" *as a way to patch the client*. This ADR is not that: it changes nothing in Wow.exe or any Windows code. But it does load our code into the macOS process that hosts Wow.exe, so it gets its own decision.

## Decision
1. A small **x86_64 macOS dylib** (the shim, ~120 lines of Objective-C, source in the repo, built by `hs app`) is loaded into the Wine host process via `DYLD_INSERT_LIBRARIES`. It's set only in `launch.wine_env()` for runs started by the app (bash strips DYLD vars, so it can't come from a script), never globally.
2. Under the app, the shim:
   - sets the host process to an accessory app (no Dock icon, no menu bar);
   - sets its display name to "Hearthstone" and its icon to our `AppIcon.icns`;
   - makes Wine's `transformProcessToForeground:` run once (idempotent);
   - listens for a `local.hearthstone.reopen` notification to unminimise the game window.

   From `./play` in a terminal: name + icon only, so the game keeps a normal Dock icon.
3. The launcher stays the one Dock icon. It hands focus to the game when the window appears, and forwards reopen/activate (Dock click, Finder, Cmd-Tab) to it. Quit from the Dock uses the existing shutdown path.
4. **Fallback setting:** `HSDOCK_POLICY = accessory | regular`. `regular` gives option B (the game keeps a Hearthstone-labelled Dock icon, Wine's menus and native full screen intact).
5. **Scope limits:**
   - the shim never touches Wow.exe, the Windows side, rendering (mtld3d) or `client/`;
   - it lives outside `client/` and outside the pinned runtime directory `env/wowsilicon-r17/` (whose hash stays unchanged);
   - a runtime bump re-runs the Dock checks.

## Consequences
- One Hearthstone icon from launch to quit; clicking it or Cmd-Tabbing brings the game forward.
- Lost under the accessory policy: Wine's menu bar shortcuts (Cmd-H, Cmd-Opt-Q, Wine's Cmd-Opt-Ctrl-F full screen). WoW's own window/full-screen setting and Alt-Enter are unaffected. Native green-button full screen is unverified; if it fails, use `regular`.
- Uses `_LSSetApplicationInformationItem` (private API) for the name. Acceptable for a personal, never-distributed app, but a macOS update could break it. The Dock checks catch that, and the failure mode is cosmetic (back to two icons), not a broken game.
- Works because the r17 `wine` binary isn't hardened-runtime signed. A future signed runtime would silently ignore the shim (graceful).
- Performance: the shim runs only at window creation and on reopen/unminimise, with no per-frame work. `app.shim_perf` measures startup time and CPU with vs without it.
