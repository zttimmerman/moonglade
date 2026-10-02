# ADR-0004: Wine layer: Whisky's bundled Wine with a repo-local prefix

- Status: proposed (the launch path is built. A clean `client.launch_smoke` PASS is still blocked on a damaged client source; see Consequences.)
- Date: 2026-10-01

## Context
CLAUDE.md §2 fixes the client as the Windows 3.3.5a (12340) client "run under CrossOver/Wine". CrossOver isn't installed on this Mac, and it's paid. Whisky.app 2.3.4 is installed. Whisky is a free Wine front-end, and upstream no longer maintains it. It downloaded its own Wine build to `~/Library/Application Support/com.isaacmarovitz.Whisky/Libraries/Wine/`: `bin/wine64`, `bin/wineserver`, `wine64 --version` = `wine-7.7`, `WhiskyWineVersion.plist` = 2.4.5. This is a CrossOver-derived Wine with 32-on-64 support (`lib/wine/x86_32on64-unix`, `i386-windows`). It also contains `lib/external/D3DMetal.framework`. Zach has two Whisky bottles of his own under `~/Library/Containers/com.isaacmarovitz.Whisky/Bottles/`. Those count as user data (§11).

The binaries are x86_64 and run under Rosetta 2 (`arch -x86_64 /usr/bin/true` works).

## Decision
- **Wine binaries:** call Whisky's `wine64` and `wineserver` directly, read-only, from the path above (`paths.WINE_BIN`). The harness never starts Whisky.app, never changes Whisky's settings, and never opens or creates a Whisky bottle.
- **Prefix:** a repo-local prefix at `env/wineprefix` (`paths.WINEPREFIX`; `env/` is gitignored). `hs launch` creates it on first use with `wine64 wineboot --init`. Every Wine call sets `WINEPREFIX` to that path explicitly, so it can't fall back to `~/.wine` or reach a bottle.
- **Environment:** `harness/hslib/launch.py:wine_env()` strips any inherited `WINE*`, `DXVK_*`, `MVK_*`, `MTL_*` and `D3DM_*` variables. It then sets only what Whisky sets for a default bottle:
  - `WINEPREFIX=<repo>/env/wineprefix`
  - `WINEDEBUG=fixme-all`
  - `WINEMSYNC=1`: msync is Whisky's default `enhancedSync`. Zach's two bottles' `Metadata.plist` (read, not modified) both have `enhancedSync = msync`, `dxvk = false` and `windowsVersion = win10`. `env/logs/wine.log` shows `msync: up and running`, so this Wine build honours it.
  - **No `WINEDLLOVERRIDES`.** Whisky sets `dxgi,d3d9,d3d10core,d3d11=n,b` only when a bottle has DXVK on. That string is in the `Whisky.app/Contents/MacOS/Whisky` binary next to `WINEDLLOVERRIDES`, `DXVKHUD`, `ROSETTA_ADVERTISE_AVX`, `METAL_CAPTURE_ENABLED` and `D3DM_SUPPORT_DXR`, all found with `strings`. DXVK is off by default. Whisky's DXVK folder (`Libraries/DXVK/x32`) has only `dxgi`, `d3d10core` and `d3d11`, with no `d3d9`. WoW 3.3.5a renders with Direct3D 9, so it uses Wine's builtin `d3d9` (wined3d on OpenGL). A diagnostic run's crash report lists `OPENGL32.dll` and `winemac.drv` as loaded. D3DMetal covers D3D11/12 only and isn't used. The HUD, Metal-capture, AVX and DXR toggles are opt-in debugging and performance switches, so they're left off.
  - Source caveat: Swift stores strings of 15 bytes or fewer inline in arm64 code, so short names such as `WINEDEBUG`, `fixme-all` and `WINEMSYNC` aren't visible to `strings`. Those values come from Whisky's open-source WhiskyKit (`Wine.swift` environment construction, `BottleSettings.swift` `environmentVariables`) and from the bottle plists above. They weren't re-verified byte-for-byte in this binary.
- **Process:** `hs launch` runs `wine64 Wow.exe` with cwd `client/`, detached (`start_new_session`), with stdout and stderr appended to `env/logs/wine.log`. Stopping uses `wineserver -k` with this `WINEPREFIX`, which only affects this prefix's server.
- **Process identity (for `--smoke` and orphan checks):** a process belongs to this prefix if (1) `lsof` shows it holding a file in Wine's per-prefix server dir `/tmp/.wine-<uid>/server-<st_dev hex>-<st_ino hex>` of `env/wineprefix`, which covers every Windows process; or (2) `ps -E` shows `WINEPREFIX=<repo>/env/wineprefix` exactly, which covers `wineserver` and the `wine64` launcher; or (3) it's a Whisky-Wine process whose command line runs `Wow.exe`. Zach's bottles have a different server dir and `WINEPREFIX`, so they never match.
- **Client config (Wine stability):** `clientcfg.WINE_DEFAULTS` holds `SET gxWindow "1"` and `SET gxApi "d3d9"`. Windowed mode avoids fullscreen display-mode switches, a common Wine-on-macOS failure point. `d3d9` pins the client's default Windows renderer through wined3d; `opengl` is the alternative if d3d9 misbehaves. `hs launch` only adds these when the key is missing from `WTF/Config.wtf`, so a setting Zach later changes in-game is kept. `realmlist` and `patchlist` are still enforced as before. No binary is touched (§2). `hs launch` compares every manifest `.exe`/`.dll` SHA-256 before and after a run.

## Alternatives
- **CrossOver** (CodeWeavers, paid, maintained): the fallback if Whisky's Wine 7.7 can't run the client, or once a macOS update breaks it. Use the same repo-local idea (a dedicated bottle) and point `paths.WINE_BIN` at CrossOver's wine.
- **Homebrew `wine-stable` / Gcenx builds:** more moving parts, and no 32-on-64 guarantee on Apple Silicon. Not tried.
- **Using a Whisky bottle:** rejected. Bottles hold Zach's data, and Whisky's UI owns their settings.

## Consequences
- Upstream doesn't maintain Whisky. Its Wine is frozen at 7.7 (CrossOver-based), with no fixes for future macOS or Rosetta changes. If Apple restricts Rosetta 2, which runs every x86_64 Wine binary here, this layer stops working, and CrossOver (or a native ARM Wine) becomes necessary.
- The Wine binaries live outside the repo and aren't pinned by hash. A Whisky reinstall could change them. `hs launch` fails clearly if they're missing.
- First launch creates the prefix (`wineboot --init`, about 26 s here).
- **Status at writing:** the [redacted] zip (`~/Downloads/[redacted]_3.3.5a.zip`) is damaged. Two of its 8428 torrent pieces fail their SHA-1 against Transmission's `.torrent`: piece 6748 is all zeros, and piece 6950 has wrong data. As a result, `Data/enUS/locale-enUS.MPQ` and `Data/expansion.MPQ` fail their zip CRC. `hs client-install` refuses to write a manifest. In a diagnostic run with the damaged MPQs extracted (and then removed), Wine started `Wow.exe`. Wine loaded the graphics, input and DivX modules, and the client failed with its own `ERROR #121 Version Mismatch: Unable to open DBFilesClient\Startup_Strings.dbc`, a data-file error. The client never got far enough to test the Wine graphics path. The smoke test can't pass until the source is repaired (Transmission "Verify Local Data") and `hs client-install` is re-run.
