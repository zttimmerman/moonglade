---
name: hs-client-debugging
description: Diagnose the WoW 3.3.5a client under Wine on macOS — crashes, rendering bugs, launch failures. Use when the game crashes, renders wrong, won't start, or `client.launch_smoke` fails.
---

# Client debugging (Wine on macOS)

## Where to look
- `<client>` below is `env/client-play/` (the patched play client, what `./play` and the app run) or `client/` (the pristine stock client, `hs launch`).
- **Crash reports:** `<client>/Errors/<date> Crash.txt` (exception code, EIP, registers, per-thread stacks, loaded modules). Map EIP to a module with the `DBG-MODULE<base size "name">` lines. An EIP in no module means dynamically loaded code (e.g. Warden modules → disabled server-side) or Wine thunks.
- **Wine log:** `env/logs/wine.log`; each launch is stamped `===== [hs launch] … start`. Wine errors look like `err:<channel>`; mtld3d logs `INFO/WARN/ERROR mtld3d::…`.
- **Client logs:** `<client>/Logs/` (e.g. `FrameXML.log`, `connection.log`; `gx.log` with renderer/pixel-format details appears for some renderer runs, e.g. `gxApi opengl`).
- **Server side:** `env/logs/Server.log`, `Errors.log`, `worldserver.err`.

## Tools
- More Wine detail: `HS_WINEDEBUG="fixme-all,fixme+d3d_shader,warn+d3d_shader,warn+d3d" harness/hs launch`. Shader link failures print the GLSL source and info log.
- Wine layers: `HS_WINE=mtld3d` (default; WoWSilicon r17 + mtld3d, D3D9→Metal), `HS_WINE=whisky` (Wine 7.7 wined3d, for A/B). Each has its own prefix under `env/`.
- **Screenshot one window** (needs Screen Recording permission for the terminal): get the window number for the Wow.exe pid with JXA `CGWindowListCopyWindowInfo`, then `screencapture -x -o -l <wid> out.png`, and Read the PNG. Process names differ by layer: Whisky shows `wine64-preloader Wow.exe`, Wine 11 shows `Z:\…\client\Wow.exe`.
- Process discovery: `launch.prefix_processes()` (open files under the prefix or its server dir; path-boundary aware). Clean up with `launch.stop_prefix()` (wineserver -k).

## Known causes (2026-10)
- ILLEGAL_INSTRUCTION at a non-module EIP right after login → Warden; it's disabled in `config.py`.
- Red/blue smears in the world → a Wine 7.7 wined3d clip-plane GLSL bug (`ps_link` undeclared); fixed by the mtld3d layer (ADR-0004).
- The client's `gxApi opengl` fails on winemac ("pixelFormat does not match requested").
- A new prefix stuck on a "Wine Mono" dialog → `mscoree=d;mshtml=d`; a half-built prefix → no `syswow64/kernel32.dll`, "could not load kernel32.dll c0000135".
- EULA "Accept" greyed → scroll the text to the bottom (not a hang).

## Rules
- Never modify client binaries (§2). Config via `clientcfg` only. Analysis via `hs re` (read-only, ADR-0005).
- Don't kill Zach's live game session; ask first. Smoke tests kill the prefix.
