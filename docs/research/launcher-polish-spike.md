# Launcher polish spike: one Dock icon, and a loading screen with stages

Status: **research done, nothing implemented.** No change to `Wow.exe`, `client/`, the game prefix, or the harness. Jump to [Recommendation](#recommendation).

Problem (Zach's play test, 2026-10-04): opening `Hearthstone.app` puts **two** icons in the Dock, the Hearthstone launcher and Wine's process running `Wow.exe`. Clicking Hearthstone says "already running", and clicking the Wine icon focuses the game. Zach wants Hearthstone to be the only visible app, with a click on it bringing the game to the front. He also wants a loading window with a progress bar and the current stage, the way Guild Wars 2 does it.

Labels: **verified** means read in source at the cited line or measured in an experiment below. **Inferred** means reasoned from source but not exercised. Wine source is the `wine-11.13` tag of the GitHub mirror, the upstream base of our runtime (ADR-0004: WoWSilicon r17 = WineAndAqua `wine-11.13-macos`). Links are written as `file:line` against `https://github.com/wine-mirror/wine/blob/wine-11.13/dlls/winemac.drv/`.

## Experiments (how the evidence below was produced)

All of these ran in a **scratch** prefix (`<worktree>/env/spike-wine/prefix`, made with `wineboot --init`). They used the real r17 runtime from `env/wowsilicon-r17` read-only, with exactly the `mtld3d` layer environment from `launch.py:wine_env()`. The test program was Wine's built-in `notepad`, never `Wow.exe` or the game prefix. The tools were small throwaway programs, deleted afterwards. The key one, the shim, is reproduced in the [appendix](#appendix-the-shim-used-in-e4):
- `probe`: prints `NSRunningApplication` (name, activation policy, bundle, executable, active) for a pid, and can activate it.
- `ToyLauncher.app`: an ad-hoc-signed AppKit bundle that models the proposed launcher. It starts as a Regular app with a window, switches to Accessory after N s and hands activation to a target pid. It logs `applicationShouldHandleReopen` and `applicationDidBecomeActive` and forwards activation from both.
- `hsdock.dylib`: an x86_64 dylib loaded into the Wine host processes with `DYLD_INSERT_LIBRARIES`. Once AppKit is loaded it swizzles `-[NSApplication setActivationPolicy:]` and `-[NSApplication setApplicationIconImage:]`, and it sets the LaunchServices display name.
- `lsappinfo info -app <pid>` (built into macOS) shows LaunchServices' own view: `LSDisplayName` and `ApplicationType` (Foreground = Regular, UIElement = Accessory).

| # | What | Result |
|---|---|---|
| E1 | Plain `notepad` under r17 | Its process is `NSRunningApplication` name **`wine`**, policy **regular**, no bundle id or bundle URL, executable `…/lib/wine/x86_64-unix/wine`. A second Wine process (`explorer.exe /desktop`) is registered too, but as **prohibited** (no Dock icon). |
| E2 | `activateWithOptions:` on notepad from a background CLI | Returns YES, but the frontmost app does **not** change. macOS 14+ cooperative activation ignores activation requests from apps that aren't active. |
| E3 | ToyLauncher → Accessory → hand off to notepad, then `open -a ToyLauncher.app` again | Hand-off works: notepad becomes frontmost 1 s later, and the toy reports `accessory` from outside. **The reopen turned the toy back into a Regular app before `applicationShouldHandleReopen` ran** (`didBecomeActive: … policy=regular`). Forwarding activation from that handler brought notepad back in front. Switching back to Regular without a reopen also works (`setActivationPolicy(regular) -> 1`). |
| E3b | Toy switches to Accessory with no target | The toy loses activation, and the previously active app (the terminal) comes forward. |
| E4 | notepad with `hsdock.dylib`, `HSDOCK_POLICY=accessory`, `HSDOCK_NAME=Hearthstone`, `HSDOCK_ICON=branding/AppIcon.icns` | Notepad's process is `name=Hearthstone policy=accessory`. Its window is on screen (924×697, layer 0) and has **no Dock icon**. The shim log shows `setActivationPolicy:0 -> using 1`, then `setApplicationIconImage: exe icon (replaced=1)`, then `set display name 'Hearthstone' … status=0`. The accessory notepad did **not** bring itself to the front. ToyLauncher's hand-off made it frontmost, and so did a later reopen of the toy. |
| E4b | Same, but `HSDOCK_POLICY=regular` (name and icon only) | `lsappinfo`: `"LSDisplayName"="Hearthstone"`, `"ApplicationType"="Foreground"`, `LSBundlePath=NULL`. It has a normal Dock icon, labelled Hearthstone. |
| E5 | `DYLD_INSERT_LIBRARIES` reach | `DYLD_PRINT_LIBRARIES` shows the dylib loading into the first `wine` process and into every child Windows process (each one is a separate `x86_64-unix/wine` exec). The r17 `wine` binary is **not code-signed** (`codesign -dv`: "not signed at all"), so dyld honours `DYLD_*`. Gotcha: **SIP strips `DYLD_*` from the environment of protected binaries such as `/bin/bash`**. The variable therefore has to be set by the process that `exec`s `wine`, which in our case is Python's `Popen` in `launch.py:wine_env()`. That's where `DYLD_LIBRARY_PATH` already lives, and it works for the same reason. |

Not done: no screenshot of the Dock. Capturing it would need Screen Recording permission, and TCC is off limits. `NSRunningApplication.icon` of an unbundled process returns the generic "exec" file icon, not the live Dock tile. So **what the Dock tile looks like is from source, not observed** (marked inferred below).

## Q1: how Wine's Mac driver puts a Windows process in the Dock

**Where the Dock icon comes from: the `.exe`'s own icon resource, applied at runtime.** *(verified in source)*
- Each Windows process that opens a window runs `macdrv_SetDesktopWindow`, which calls `pthread_once(&app_icon_once, set_app_icon)` (`window.c:1197`, `:1234`). `set_app_icon` (`window.c:795-802`) calls `create_app_icon_images()` (`image.c:250-299`). That calls back into the PE side, `macdrv_app_icon` (`dllmain.c:255-`). The callback takes **the first `RT_GROUP_ICON` of the main module** (`EnumResourceNamesW(NULL, RT_GROUP_ICON, get_first_resource, …)`, `dllmain.c:269`; `get_first_resource` stops at the first one, `dllmain.c:237-249`) and turns every size into a CGImage.
- `setApplicationIconFromCGImageArray:` stores the result as `applicationIcon` (`cocoa_app.m:1057-1091`). `transformProcessToForeground:` then calls `[NSApp setApplicationIconImage:self.applicationIcon]` (`cocoa_app.m:304`). For `Wow.exe` the Dock tile is therefore WoW's own icon. *(inferred: that's the first icon group in Wow.exe; not looked up in the resources)*
- **No bundle is involved.** The r17 runtime has no `.app`, `Info.plist` or `.icns` anywhere (`find env/wowsilicon-r17 -name '*.plist' -o -name '*.icns' -o -name '*.app'` finds nothing). The Dock label is the executable's file name, **`wine`** *(verified, E1)*. Wine only uses a bundle name if it has a main bundle (`cocoa_app.m:248`, for the "Hide/Quit <name>" menu items).

**When it appears:** `transformProcessToForeground:` runs whenever a Wine window is ordered on screen (`cocoa_window.m:1783-1793`). If the policy isn't Regular yet, it calls `[NSApp setActivationPolicy:NSApplicationActivationPolicyRegular]` (`cocoa_app.m:223-233`), builds a "Wine" menu and a Window menu, and sets the icon. Before its first window, a Wine process has no Dock icon. Helper processes that never open a window stay prohibited (E1: `explorer.exe`). The upstream loader doesn't rename the process for the Dock either: `dlls/ntdll/unix/loader.c` has no `LSSetApplicationInformation`/display-name code, only the CFRunLoop main-thread setup (`loader.c:1903-1945`).

**Registry or env options for the Dock: none.** *(verified)*
- `setup_options()` (`macdrv_main.c:279-385`) reads `HKCU\Software\Wine\Mac Driver` and the per-app `HKCU\Software\Wine\AppDefaults\<app.exe>\Mac Driver`. The complete key list is: `WindowsFloatWhenInactive`, `CaptureDisplaysForFullscreen`, `AllowVerticalSync`, `AllowSetGamma`, `Left/RightOptionIsAlt`, `Left/RightCommandIsCtrl`, `AllowSoftwareRendering`, `AllowImmovableWindows`, `UseConfinementCursorClipping`, `CursorClippingLocksWindows`, `UsePreciseScrolling`, `OpenGLSurfaceMode`, `EnableAppNap`, `RetinaMode`. None of them hides the Dock icon, sets an accessory/UIElement policy, or sets the icon or name.
- The r17 `winemac.so` matches. Its strings show only the same `Software\Wine\Mac Driver` keys, plus the Cocoa selectors `setActivationPolicy:`, `setApplicationIconImage:`, `applicationDockMenu:` and `grabDockIconSnapshotFromWindow:force:`. There's no CrossOver-style extra option. `gh search code setActivationPolicy --repo WineAndAqua/wine` returns only the same upstream line.
- An `LSUIElement` in a bundle wouldn't help either: Wine sets Regular explicitly (`cocoa_app.m:233`), which overrides the plist.

**Wine's own Dock behaviour, useful later:**
- Reopen: a click on Wine's own Dock icon calls `applicationShouldHandleReopen:`, which calls `unminimizeWindowIfNoneVisible` (`cocoa_app.m:2230-2236`, `:2118`). That's why clicking the Wine icon focuses the game today.
- Quit from Wine's Dock menu: `applicationShouldTerminate:` (`cocoa_app.m:2238-`) sends `APP_QUIT_REQUESTED`. That turns into `WM_QUERYENDSESSION`/`WM_ENDSESSION` to the process's windows (`window.c:2116-2130`, `dllmain.c:183-230`). How WoW reacts to that wasn't tested.
- Activation: on macOS 14+, Wine uses cooperative activation. It posts a distributed notification asking other Wine apps to yield, then calls `[NSApp activate]` (`cocoa_app.m:1993-2030`). A Wine process that isn't active can't reliably bring itself to the front. E4 confirms this for the accessory case.

**WoWSilicon prior art:** WoWSilicon's launcher doesn't hide Wine's Dock icon either. It watches `NSWorkspace.didLaunchApplicationNotification` for a new process whose `executableURL` is under its runtime root, then calls `NSApplication.shared.yieldActivation(to:)` + `application.activate(from: .current)` (`Sources/WoWSiliconSwift/Services/LaunchService.swift:772-829`, github.com/WoWSilicon/WoWSilicon at HEAD 2026-10-04). That's the same hand-off as in E3/E4.

## Q2: what our launcher can do on its own (no runtime change)

| | Can it? | Evidence |
|---|---|---|
| (a) Hide its own Dock icon once the game is up (Accessory), restore it later | **Yes.** `setActivationPolicy:Accessory` removes the icon; `Regular` restores it. **Caveat:** a LaunchServices reopen (Finder double-click, Dock click on a kept icon, `open -a`) automatically turns an Accessory app back into Regular before `applicationShouldHandleReopen:` runs. The launcher has to switch back to Accessory after forwarding. That shows up as a Dock icon flicker. | E3, E3b |
| (b) On reopen, bring the game forward | **Yes, but only from the active launcher**: `[NSApp yieldActivationToApplication:wine]` then `[wine activateFromApplication:NSRunningApplication.currentApplication options:…]`. It works from `applicationShouldHandleReopen:` and from `applicationDidBecomeActive:`. A plain `activateWithOptions:` from a background process is ignored. | E2, E3, E4 |
| (c) Find the Wine process's `NSRunningApplication` | **Yes, by pid.** The `NSRunningApplication` pid **is** the Windows program's unix pid: notepad's pid printed as `C:\windows\system32\notepad.exe` in `ps`. The play loop already finds that pid (`launch.wow_processes()`, `launch.py:155-157`), so it can pass it on. Fallback (WoWSilicon's way): the first non-prohibited `NSRunningApplication` whose `executableURL` is `<runtime>/lib/wine/x86_64-unix/wine` and that wasn't running before the launch. | E1, E4 |

**What Zach would then see in the Dock (launcher only, option C below):** one running icon, Wine's, labelled **`wine`**, showing **WoW's own icon** (from `Wow.exe`, Q1). If Zach keeps Hearthstone in the Dock, its kept tile also stays visible, with no running dot. Clicking that tile reopens the launcher, which flips it back to Regular (E3), hands focus to the game and hides again. Cmd-Tab shows `wine` with the WoW icon.

## Q3: can the Wine process show the Hearthstone name and icon without touching Wow.exe?

Two categories, kept separate on purpose:
- **Our Wine runtime layer** (the r17 copy in `env/`, the environment we pass to `wine`, anything loaded into the macOS host process). This is not a client binary. ADR-0004 already owns it.
- **`Wow.exe` and the client files.** Changing these needs an ADR and Zach's yes (CLAUDE.md §2; ADR-0009 approved only patch 0001).

| Option | Name | Icon | Dock icon hidden? | Category | Status |
|---|---|---|---|---|---|
| **Shim dylib** (`DYLD_INSERT_LIBRARIES`, swizzles `NSApplication`) | **Yes** (`_LSSetApplicationInformationItem(_kLSDisplayNameKey)`, `LSDisplayName=Hearthstone`) | **Yes** (replaces the image Wine passes to `setApplicationIconImage:`) | **Yes, optional** (Regular → Accessory) | Runtime layer: a new file loaded into Wine's macOS host process via the environment. Wow.exe, its files and its Windows-side memory are untouched. | **Verified** with notepad (E4, E4b, E5) |
| Wrap the runtime in an app bundle (`Info.plist` + `.icns`) | Inferred yes (`CFBundleName`) | **No.** Wine still replaces the bundle icon with the exe icon (`cocoa_app.m:304`) | **No.** Wine still forces Regular (`cocoa_app.m:233`) | Runtime layer, but a restructured copy: every Windows process execs `wineloader = <realpath of ntdll.so's dir>/wine` (`loader.c:380-406`, `:458-469`). A symlinked bundle is bypassed, so `x86_64-unix/` itself would have to live in `X.app/Contents/MacOS`. That breaks `dll_dir`/`bin_dir`/`data_dir` derivation (`loader.c:396-399`) unless more symlinks paper over it. | Not tried; fragile, small gain |
| Patch or rebuild `winemac.so` (e.g. an env switch for policy/icon/name) | Yes | Yes | Yes | Runtime layer, but we'd own a Wine build (or a binary-patched `.so`) on top of WoWSilicon's pinned artifact | Not tried; most maintenance |
| `HKCU\…\Mac Driver` keys | No | No | No | n/a | Don't exist (Q1) |
| Change Wow.exe's icon resource | No | Yes | No | **Client binary change: new ADR + Zach's yes** | Rejected: not needed |

**Answer: no change to `Wow.exe` is needed for any of this.** The shim reaches everything Zach asked for, from the runtime layer.

**Is the shim allowed?** It isn't client binary patching in the CLAUDE.md §2 sense: no client file changes, `client.inspect` and the before/after manifest hash keep passing, and nothing touches Windows code or memory. It does run code inside the macOS process that also hosts `Wow.exe`. ADR-0009 lists "runtime memory patching or DLL injection under Wine" as a rejected alternative *for patching the client*. To avoid any doubt it deserves **its own short ADR (ADR-0010, "Wine host shim for Dock presentation") and Zach's yes** before it's built. Scope it narrowly: AppKit presentation calls only (activation policy, Dock icon, display name, and an "unminimize" hook), loaded only from `wine_env()`, and only when `./play` runs.

## Recommendation

### Feature 1: one visible Hearthstone app

Ranked by how close each gets to "one Hearthstone; clicking it focuses the game", then by robustness and effort:

**A. Recommended: the launcher stays in the Dock, the game runs as Accessory (shim), and the launcher forwards focus.**
- Launcher: always Regular with the Hearthstone icon, so the kept/pinned tile and the running tile are the same icon. When the game window is up, it hides its loading window and hands focus to the game (`yieldActivationToApplication:` + `activateFromApplication:`). From then on, `applicationShouldHandleReopen:` (Finder/Dock click) and `applicationDidBecomeActive:` (Cmd-Tab, clicking the tile) forward focus to the game instead of showing "already running". The "already running" dialog stays only for a second `./play` (EXIT_BUSY) and while the game is still loading (where it now shows the loading window).
- Shim: `HSDOCK_POLICY=accessory` + `HSDOCK_NAME=Hearthstone` + `HSDOCK_ICON=<bundle>/Contents/Resources/AppIcon.icns`, set in `wine_env()` only when the play loop runs under the app. The launcher passes something like `HS_APP=1`, and `launch.py` adds `DYLD_INSERT_LIBRARIES` there, because bash would strip it (E5).
- Unminimize: the launcher can't trigger Wine's reopen handler without Apple Events, which would cost an Automation (TCC) prompt. So the shim also listens for a distributed notification, `local.hearthstone.reopen`. On it, the game process calls Wine's `applicationShouldHandleReopen:` → `unminimizeWindowIfNoneVisible` (`cocoa_app.m:2230`). *(inferred, not built)*
- Quit from the Dock: the existing path. The launcher's `applicationShouldTerminate:` sends SIGTERM to the play loop → `stop_prefix` + `stack.down` (`launcher.m:344-350`, `play.py:293-312`).
- Result: one Dock icon, Hearthstone's, from launch to quit, with no flicker or tile jumping. Cmd-Tab shows one Hearthstone entry, and selecting it focuses the game.
- Downsides and risks:
  - The game process has **no menu bar or Cmd-Tab entry of its own**. Accessory apps don't get a menu bar, so Wine's "Wine" and "Window" menus are gone, including Cmd-Opt-Q (Wine's Quit), Cmd-H (hide), Minimize/Zoom, and **Cmd-Opt-Ctrl-F (Enter Full Screen)** (`cocoa_app.m:252-300`). WoW's own in-game quit and Alt-Enter are unaffected. *(inferred; human-verify)*
  - **Native macOS full screen** (green button) for an accessory app's window is unverified. If it doesn't work, use option B.
  - Wine re-runs `transformProcessToForeground:` each time a window is ordered in, because the policy is "not Regular" (`cocoa_app.m:225`). That rebuilds the menus, re-requests activation, and leaks one `beginActivity` token each time. It's harmless for WoW's single window (E4: one call), but the shim should make it idempotent: swizzle Wine's `transformProcessToForeground:` to run once.
  - It depends on Wine's internal selector names (`setActivationPolicy:` is AppKit; `transformProcessToForeground:` is Wine's own). A runtime bump (new `wowsilicon_runtime_sha256` in `fork.lock`) must re-run the Dock checks.
  - `./play` from a terminal (no launcher) must **not** use Accessory, or the game would have no Dock icon at all. There the shim runs with name + icon only (B's look).
- Effort: about 120 lines of Objective-C for the shim (x86_64, built with `clang -arch x86_64` next to the launcher in `hs app`, output kept out of `client/`), about 60 lines in `launcher.m`, about 10 in `launch.py`, plus ADR-0010.

**B. Fallback: the game keeps its Dock icon but shows Hearthstone's name and icon (shim without Accessory), and the launcher hides itself (Accessory) once the game window is up.**
- What it looks like: after launch, one running Dock icon labelled Hearthstone with the Hearthstone icon. It belongs to the game, so clicking it focuses the game natively (Wine's reopen → unminimize). Cmd-Tab shows it, Wine's menus and native full screen stay as today, and Dock → Quit goes through Wine's quit request to WoW.
- Downsides:
  - Brief **two icons at hand-off**: Wine's icon appears when its first window orders in, and the launcher can only hide after it sees that window (about 0.5 s of polling).
  - The icon **moves** from the launcher's slot to the right-hand "running apps" area.
  - **If Hearthstone is pinned**, the pinned tile stays visible next to the game's (no dot). Clicking the pinned tile reopens the launcher, which flips it back to Regular (E3) before it can forward focus and hide again.
  - WoW's reaction to `WM_QUERYENDSESSION` from a Dock Quit is untested; the play loop's cleanup covers it either way.
  - The launcher must return to Regular before showing any error dialog.
- Effort: smaller shim (no policy part, no unminimize hook), the same launcher hand-off code, and the same ADR.

**C. No runtime change (launcher-only, works today).** As B, but the game's tile reads `wine` with WoW's icon (Q2). This needs no ADR, so it can ship first as an improvement over two icons. It doesn't meet "Hearthstone is the only visible app".

**Not recommended:** the bundle-wrapped runtime (fragile layout; can't change the icon or hide the tile), a rebuilt or patched `winemac.so` (we'd own a Wine build), and any `Wow.exe` change (unneeded).

Rollout suggestion: build C's launcher hand-off and the loading stages first, with no gate. In parallel, write ADR-0010 for the shim. On Zach's yes, add A, with B as a one-setting fallback (`HSDOCK_POLICY` regular vs accessory) in case Zach dislikes losing native full screen.

### Feature 2: a loading window with stages and progress

**What exists today** *(verified)*
- The launcher follows `[play] …` lines that `./play` writes to stdout, which goes to `env/logs/app.log`. It tails that file every 0.5 s and shows the latest line as the window's detail text. It hides the window on `The game is starting…` (`launcher.m:217-247`). The window has an indeterminate spinner (`launcher.m:108-112`). In test mode every status is also written to the hook file as JSON (`launcher.m:57-75`).
- Steps the play loop goes through, and how measurable each one is:

| Step | Code | Measurable? | Typical duration |
|---|---|---|---|
| lock, client present, Wine present | `play.py:339-343`, `launch.py:327-340` | instant | <1 s |
| database (mysqld) | `stack.mysql_up` (`stack.py:137-162`), polls `mysql_ready` | done/not done; first run initialises the datadir | a few s *(not measured here)* |
| login server | `stack.auth_up` (`stack.py:232-248`), waits for port 3724 | done/not done | ~1 s *(not measured)* |
| fresh world (bots re-rolled) | `population.fresh_start` (`stack.py:348-350`) | done/not done | short |
| world server | `stack.world_up` (`stack.py:250-268`), polls SOAP every 2 s | **determinate**: `worldserver.out` grows through ~880 lines in a fixed order (`Loading World Information…`, …, `Starting Map System`, …, `Initializing mod-playerbots`, …, `WORLD: World Initialized In 0 Minutes 14 Seconds` at line 878 of today's log) | ~16 s warm (`ready_s` 16.0–16.1 in the last full run, `env/logs/b18-full-results.json`) |
| first-ever world start (DB import) | `stack.up` timeout 1800 s when `first_run` (`stack.py:338-339`) | indeterminate | minutes |
| AI players logging in | not waited for today. Measurable as `population.online_bots()` (`population.py:141-143`) against the target N (`config.population_n`) | **determinate n/N** | ramp not measured (bots log in after the world is up) |
| your account | `ensure_player_account` via SOAP (`play.py:158-172`) | done/not done | <1 s |
| client config + Wine prefix | `clientcfg.configure`, `launch.ensure_prefix` (`launch.py:208-225`) | first-ever prefix: indeterminate (`wineboot`, ~26 s per ADR-0004) | <1 s normally |
| client started | `launch.start_client` (`play.py:257-264`) | done | – |
| game window up | not tracked by the play loop. Measurable: `launch.onscreen_windows(pid)` (`launch.py:280-288`), or natively in the launcher with `CGWindowListCopyWindowInfo` (owner pid + bounds need no Screen Recording permission) | done when a layer-0 window ≥640×480 exists | ~4 s to the Wow process (`after_launch_s` 4.2) plus window creation |

**Channel: a structured line on stdout, next to the human ones.** Recommended format: `[stage] {"id":"world","label":"Loading the world","done":312,"of":880}`. Why stdout:
- It's already the launcher's input (no new IPC).
- Ordering is free.
- It lands in `app.log`, so failures keep their evidence.
- A terminal `./play` can render the same events as a text bar.
- The launcher records each one as a hook event (`{"kind":"stage",…}`), so checks get them for free.

A status file (`env/run/play-status.json`) would lose ordering and intermediate states. The test hook is the launcher's *output*, not an input from the play loop, so it isn't the channel either. The existing `[play]` lines stay unchanged: they remain the detail text and the terminal output.

**Stage list (ids are the contract; labels are Zach-facing):**

| id | label | progress inside the stage |
|---|---|---|
| `prepare` | Getting ready | instant |
| `database` | Starting the database | indeterminate (done/not done) |
| `login` | Starting the login server | indeterminate |
| `world` | Loading the world | **lines of `worldserver.out` / the line count of the last successful boot** (stored in `env/run/boot-calibration.json`; ~880 today). On the first ever boot, or a DB import: indeterminate, with the label "Building the world (first run, this takes a while)" |
| `bots` | Waking *N* AI players | `online_bots()/N`. See the note below |
| `account` | Checking your account | instant |
| `game_setup` | Preparing the game | instant (first prefix: indeterminate) |
| `launching` | Launching the game | indeterminate, until the client window is up |
| `ready` | (window hands off to the game) | – |

When the server is already running, `database`/`login`/`world` are emitted as `skipped` at once, and the bar jumps ahead.

**Progress model:** a determinate overall bar. Each stage gets a weight equal to its share of the **last run's measured durations** (stored with the calibration, default weights from the table above: world ≈ 60 %, launching ≈ 15 %, the rest small). The overall value is the sum of finished weights plus the current weight × its fraction. Indeterminate stages creep toward 90 % of their slot on a time curve, so the bar never looks frozen. The bar never moves backward. Below the bar: the stage label (bold) and the latest `[play]` line (detail), as now.

**Bots stage, a decision for Zach:** the play loop launches the client as soon as the world answers SOAP; bots log in afterwards. Options:
1. Don't wait. Show "N AI players arriving" only as text during `launching`.
2. Wait until at least X % are online or 30 s pass. The world feels populated on arrival, at the cost of some seconds.

The ramp time wasn't measured in this spike. Measure it in the change before choosing.

## Draft spec scenarios

For a change such as `launcher-polish` (deltas to `openspec/specs/mac-app/spec.md`, plus a small `play` delta). Checks marked *GUI* need a logged-in, unlocked GUI session, as `client.launch_smoke`'s window check already does. They use a GUI stand-in client: Wine's built-in `notepad` under the same runtime and shim, started through `--client-cmd`-style plumbing. They never use `Wow.exe`.

**Requirement: one Hearthstone in the Dock**
1. *Game has no Dock icon of its own when opened from the app (A).* GIVEN the app opened with the GUI stand-in, WHEN the stand-in's window is up, THEN `lsappinfo` reports the stand-in pid as `ApplicationType=UIElement` with `LSDisplayName=Hearthstone`, the launcher pid as `Foreground` with bundle id `local.hearthstone.app`, and no other process of this prefix as `Foreground`. Check: `app.single_dock_icon` (automated, GUI).
2. *Clicking Hearthstone brings the game forward.* GIVEN the stand-in window is up and another app is frontmost (the check opens Finder), WHEN `open -a dist/Hearthstone.app` runs (the same LaunchServices reopen as a Finder or Dock click), THEN within 2 s the frontmost app's pid is the stand-in's, the hook has **no** `already_running` dialog, and the launcher is still `Foreground` (A) or back to `UIElement` (B). Check: `app.reopen_focuses_game` (automated, GUI).
3. *Activating Hearthstone by any route focuses the game.* GIVEN the same, WHEN the launcher becomes active (the check makes it active the way 2 does, then records `applicationDidBecomeActive` forwarding in the hook), THEN the stand-in is frontmost. Check: part of `app.reopen_focuses_game`. The Cmd-Tab keystroke itself is [human-verify] (synthesising keystrokes needs Accessibility permission, and TCC is off limits).
4. *Hand-off when the game window appears.* GIVEN the app opened, WHEN the stand-in's first on-screen window appears, THEN the loading window is hidden and the stand-in becomes frontmost within 2 s, without user input. Check: `app.handoff_focus` (automated, GUI).
5. *A minimised game comes back on click (A).* GIVEN the stand-in window minimised, WHEN the app is reopened, THEN the window is on screen again. Automated if the shim gets a test-only "minimise" notification (`app.reopen_unminimizes`), otherwise [human-verify]: macOS has no permission-free way for the harness to minimise another app's window.
6. *Terminal `./play` keeps a normal Dock icon with Hearthstone's identity.* GIVEN `./play --client-cmd <GUI stand-in>` from a shell (no app), WHEN the window is up, THEN the stand-in pid is `Foreground` with `LSDisplayName=Hearthstone`, and the shim log records the icon replacement. Check: `play.dock_identity` (automated, GUI). What the icon looks like: [human-verify].
7. *The shim stays inside the runtime layer.* GIVEN any play run, THEN `client.inspect` passes, the manifest hashes before and after match (existing), the shim file lives outside `client/` and outside `env/wowsilicon-r17/` (its pinned hash in `env/wowsilicon-r17/.hs-runtime-sha256` is unchanged), and `DYLD_INSERT_LIBRARIES` comes only from `launch.wine_env()` and isn't set globally (`launchctl getenv DYLD_INSERT_LIBRARIES` is empty). Check: `app.shim_scope` (automated, quick tier for the static parts).
8. *Quit from the Dock stops everything.* GIVEN the stand-in up, WHEN the launcher is asked to quit, THEN everything stops as in "Open, play, quit". Automated via SIGTERM (existing `app.launch_lifecycle` path). The Dock's Quit menu itself is [human-verify].
9. *Zach's look and feel.* WHEN Zach plays a session, THEN he sees exactly one Hearthstone icon in the Dock with the logo, a click on it from another app brings the game back, Cmd-Tab shows one Hearthstone, and full screen still works. [human-verify]: Dock appearance, Cmd-Tab and full screen can't be observed without Screen Recording or Accessibility permission.

**Requirement: loading window with stages**

10. *Stages in order with monotonic progress.* GIVEN the stack down, WHEN the app is opened with the stand-in, THEN the hook's `stage` events use only the documented ids, appear in the documented order (`prepare, database, login, world, [bots], account, game_setup, launching, ready`), and have a non-decreasing overall progress that starts at 0 and ends at 1. Check: `app.loading_stages` (automated, full tier).
11. *The world stage shows real progress.* GIVEN a cold worldserver start, THEN at least three distinct `world` fractions strictly between 0 and 1 are emitted before `world` is done, each matching the `worldserver.out` line count at the time, within one poll. Check: `play.world_progress` (automated).
12. *A running server skips the server stages.* GIVEN the stack up, WHEN the app is opened, THEN `database`, `login` and `world` are emitted as `skipped` within 2 s of start. Check: `app.loading_stages_warm` (automated).
13. *The bots stage counts real bots* (if Zach picks option 2). GIVEN the population profile with N bots, THEN the `bots` stage reports `done` = `online_bots()` (DB) and `of` = N, and it ends at or above the threshold or at the timeout. Check: `play.bot_stage` (automated, slow tier).
14. *Failures name the stage.* GIVEN the world can't start (`--world-datadir` pointed at a bad path), THEN the stage events stop at `world`, and the error dialog's text names "Loading the world" and the log path. Check: an extension of `app.startup_failure` (automated).
15. *Terminal output stays readable.* GIVEN `./play` in a terminal, THEN every `[play]` line is unchanged, `[stage]` lines parse as JSON, and the existing `play.*` checks pass. Check: `play.stage_lines` (automated).
16. *Looks like a game loader.* WHEN Zach opens the app, THEN he sees the logo, a progress bar that moves steadily, the current stage in words, and no terminal. [human-verify]: visual quality.

## Appendix: the shim used in E4

The core of the throwaway `hsdock.m`, compiled with `clang -arch x86_64 -fobjc-arc -dynamiclib -framework Foundation` (macOS SDK, x86_64 because r17's Wine runs under Rosetta). It doesn't link AppKit. It waits for AppKit to load, so non-GUI Wine processes (wineserver, services) are unaffected.

```objc
static BOOL hs_setPolicy(id self, SEL _cmd, long policy) {          // -[NSApplication setActivationPolicy:]
    long want = (policy == 0 /*Regular*/ && env_is("HSDOCK_POLICY", "accessory")) ? 1 /*Accessory*/ : policy;
    return ((BOOL (*)(id, SEL, long))orig_setPolicy)(self, _cmd, want);
}
static void hs_setIcon(id self, SEL _cmd, id image) {              // -[NSApplication setApplicationIconImage:]
    id mine = getenv("HSDOCK_ICON") ? [[NSImage alloc] initWithContentsOfFile:@(getenv("HSDOCK_ICON"))] : nil;
    ((void (*)(id, SEL, id))orig_setIcon)(self, _cmd, mine ?: image);
}
static void set_display_name(void) {                                // Dock / Cmd-Tab / Force Quit label
    // private LaunchServices API, resolved with dlsym from CoreServices:
    // _LSSetApplicationInformationItem(-2 /*default session*/, _LSGetCurrentApplicationASN(),
    //                                  _kLSDisplayNameKey, CFSTR("Hearthstone"), NULL)  -> status 0 in E4
}
static void on_add_image(const struct mach_header *mh, intptr_t slide) {
    if (!swizzled && objc_getClass("NSApplication")) { swizzle(); dispatch_after(2 s, main, set_display_name); }
}
__attribute__((constructor)) static void init(void) { _dyld_register_func_for_add_image(on_add_image); }
```

Notes for the real one:
- Set the display name from inside the policy hook. That limits it to processes that actually open a window: in E4 the delayed call also ran in background helpers, which returned `-600`, harmlessly.
- Make Wine's `transformProcessToForeground:` run once in Accessory mode (Q-A risks).
- Add the `local.hearthstone.reopen` observer for unminimise.
- `_LSSetApplicationInformationItem` is private API. It's acceptable for a personal, never-distributed app, but it can break in a macOS update. The Dock checks above would catch that.
