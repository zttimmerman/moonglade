# Client setup (macOS + CrossOver)

How to run the Windows WotLK 3.3.5a (build 12340) client against the local Hearthstone server. The client binary is never patched. Only `realmlist.wtf` and `WTF/Config.wtf` are changed.

## 1. Install CrossOver

Install CrossOver for macOS from CodeWeavers. It's a paid app with a trial. Exact installer steps change between versions, so see the CrossOver docs.

## 2. Create a bottle

Create a new Windows bottle for WoW. A 64-bit Windows 10-style bottle is a reasonable default for a 32-bit 3.3.5a client. For menu names and bottle-type options, see the CrossOver docs. Remember the bottle's name, because you need its folder path in step 3.

Bottles normally live under `~/Library/Application Support/CrossOver/Bottles/<bottle-name>/`, and the bottle's `C:` drive is the `drive_c/` folder inside it. Check the CrossOver docs if your version uses a different location.

## 3. Copy in the 3.3.5a enUS client

Copy your own licensed 3.3.5a (12340) enUS client folder into the bottle, for example:

```
~/Library/Application Support/CrossOver/Bottles/<bottle-name>/drive_c/WoW335/
  Wow.exe
  Data/
    enUS/
  WTF/            (may not exist yet; client-config will create it)
```

Check the build. `Wow.exe` should report 3.3.5.12340, and the server only accepts gamebuild 12340.

## 4. Point the client at localhost

From the repo root:

```
harness/hs client-config "$HOME/Library/Application Support/CrossOver/Bottles/<bottle-name>/drive_c/WoW335"
```

This command:

- Refuses to run unless the folder contains `Wow.exe` and at least one `Data/<locale>/` folder.
- Writes `Data/<locale>/realmlist.wtf` = `set realmlist 127.0.0.1` for every locale folder it finds.
- Sets `SET realmlist "127.0.0.1"` and `SET patchlist "127.0.0.1"` in `WTF/Config.wtf`. It keeps every other line and its order, and keeps the file's existing CRLF or LF line endings.
- Backs up each file it changes once, as `*.hs-bak`. Later runs never overwrite an existing backup.
- Never touches `.exe`, `.dll` or `.MPQ` files. It prints JSON that includes the SHA-256 of `Wow.exe` before and after, which should be identical.

The command is safe to re-run. A second run reports `"changed_files": []`.

To undo it, copy the `*.hs-bak` files back over the originals. If a file had no backup, it didn't exist before, so delete it.

## 5. Launch

1. Start the server with `harness/hs up`, then run `harness/hs fixture setup`. It prints the test account (username `HSTEST`, password `hstest`) and creates the character `Hstester`.
2. In CrossOver, run `Wow.exe` from that bottle. You can do this with CrossOver's "run a Windows program" style command or by adding a launcher. See the CrossOver docs for the exact menu.
3. Log in with the harness account, pick or create a character, and enter the world.

If the client tries to contact a patch server or shows "connecting" forever, re-run `harness/hs client-config` against the same folder. Then check that the server is up with `harness/hs status`.
