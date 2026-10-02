# Client setup (macOS, Whisky's Wine, repo-local prefix)

How to install the Windows WotLK 3.3.5a (build 12340) client into this repo and run it against the local Hearthstone server. The client binary is never patched. Only `Data/<locale>/realmlist.wtf` and `WTF/Config.wtf` are changed. The Wine layer is described in [ADR-0004](adr/0004-wine-layer-whisky.md).

## Prerequisites

- **Whisky.app**, opened once so it has downloaded its Wine (`~/Library/Application Support/com.isaacmarovitz.Whisky/Libraries/Wine/bin/wine64`). The harness uses those Wine binaries directly. It never opens Whisky, never uses or changes your Whisky bottles, and never changes Whisky's settings.
- **Rosetta 2** (Whisky's Wine is x86_64): `softwareupdate --install-rosetta` if `arch -x86_64 /usr/bin/true` fails.
- **A 3.3.5a enUS client** as a zip or a folder, for example `~/Downloads/[redacted]_3.3.5a.zip`.
- **About 19 GB of free disk** for a first install. The client is about 17.9 GB.

## 1. Install the client into `client/`

```
harness/hs client-install ~/Downloads/[redacted]_3.3.5a.zip
```

This command:

- Opens the source read-only. It never moves, renames, modifies, or deletes the source. It records the source's size and mtime before and after the install.
- Refuses to start a full install with fewer than 19 GB free.
- Streams every member into `client/` (gitignored), stripping the zip's top folder so that `client/Wow.exe` exists. Each member's zip CRC-32 is verified as it's written. A damaged member isn't kept, and every damaged member is listed at the end.
- Writes `env/client-manifest.json` last, and only for a complete, CRC-clean install. The manifest holds the SHA-256 of every `.exe`/`.dll`, the MPQ list, every file's size, the Wow.exe PE version (should be `3.3.5.12340`), and the source stats.
- Runs `client-config` on `client/` (see step 2).

The command is safe to re-run. If `client/` already matches the manifest, nothing is extracted. If files are missing or differ, only those are re-extracted.

**If it reports corrupt members**, the source itself is damaged. In a torrent download that means some pieces are bad. In Transmission, select the torrent, choose **Verify Local Data**, and let it re-download the bad pieces. Then re-run the command. Only the missing files are extracted.

Check the install at any time:

```
harness/hs run --only client.inspect
```

This confirms that Wow.exe's version resource is 3.3.5.12340, that the client root has no D3D/DXGI/DirectInput wrapper DLLs, that every `.exe`/`.dll` still matches the manifest, and that the source zip is unchanged.

## 2. Point the client at localhost (done by install and launch)

`hs client-install` and `hs launch` both run this for you. To run it by hand:

```
harness/hs client-config client
```

- It refuses to run unless the folder contains `Wow.exe` and at least one `Data/<locale>/` folder.
- It writes `Data/<locale>/realmlist.wtf` = `set realmlist 127.0.0.1` for every locale folder.
- It sets `SET realmlist "127.0.0.1"` and `SET patchlist "127.0.0.1"` in `WTF/Config.wtf`. It keeps every other line and its order, and keeps the file's CRLF or LF line endings.
- `hs launch` also adds `SET gxWindow "1"` (windowed) and `SET gxApi "d3d9"`, but only if those keys aren't set yet. Change them in-game and your choice is kept.
- It backs up each changed file once, as `*.hs-bak`, and never touches `.exe`, `.dll`, or `.MPQ` files.

## 3. Launch

```
harness/hs launch
```

This starts the stack if it isn't up, runs `client-config`, creates the test account and character, and creates the Wine prefix `env/wineprefix` on first use (about 30 s). It then checks the client binaries against the manifest and starts `Wow.exe` under Wine, detached. Wine output goes to `env/logs/wine.log`. It prints the login:

- Account: `HSTEST`
- Password: `hstest`
- Character: `Hstester`

To quit, close the WoW window, or stop everything in this prefix:

```
WINEPREFIX="$PWD/env/wineprefix" "$HOME/Library/Application Support/com.isaacmarovitz.Whisky/Libraries/Wine/bin/wineserver" -k
```

`harness/hs down` stops the server stack.

## Smoke test

```
harness/hs launch --smoke
```

The smoke test launches the client, requires the Wine `Wow.exe` process to still be running after 30 s with no crash report and no `WowError.exe`, then runs `wineserver -k` for this prefix and requires that no Wine or Wow.exe process from `env/wineprefix` remains. A WoW window appears briefly. The harness check `client.launch_smoke` runs exactly this.

## Troubleshooting

- **The client shows "connecting" forever:** check `harness/hs status`, then re-run `harness/hs client-config client`.
- **The client crashes at startup:** read `client/Errors/*.txt` and the tail of `env/logs/wine.log`. `ERROR #121 ... Unable to open DBFilesClient\...` means a data file (MPQ) is damaged or missing. Run `harness/hs run --only client.inspect` and re-run `hs client-install` after repairing the source.
- **Graphics problems:** set `SET gxApi "opengl"` in `client/WTF/Config.wtf`. The harness won't override a key that's already set.
