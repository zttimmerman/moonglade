# Login-screen spike: findings (Phase A draft)

Change: `openspec/changes/login-screen-spike/`. Status: **Phase A done (read-only); Phase B (launch test) and the recommendation are still open.**

Provenance: `Wow.exe` 3.3.5a (build 12340), SHA-256 `aa63a5750d60ef16746c686b3d5e26876d98953eab08b1c026cd0faf78e88cb8`, analyzed as a copy in `$HS_RE_DIR/bin` (ADR-0005). MPQ facts come from `harness/hs mpq` (stdlib reader, `harness/hslib/mpq.py`) on the installed client, read-only. Labels: **verified** means checked against bytes, code or a recomputed hash; **inferred** means read from disassembly or naming, not confirmed at runtime.

Tooling note: the pinned Ghidra 12.1.4 ships no `decompile` native for `mac_arm_64` (only `linux_x86_64` and `win_x86_64`), so `hs re decompile` returns an error on this host. All the code reading below comes from Ghidra's **disassembly** listing, run through a scratch read-only script against the analyzed project (`-readOnly`, never saved). The pseudocode is hand-written from that listing.

## Q1: can a patch MPQ change GlueXML with an unmodified Wow.exe?

**Short answer (Phase A): no, not for anything GlueXML loads as code. Assets that GlueXML only references by name (models, textures, sounds, DBCs) are not covered by the check.** Phase B should confirm this at runtime.

### What is verified, and against what
- **Signature file:** `<toc>.sig`, built with the format string `"%s.sig"` @0x00a44228, so `Interface\GlueXML\GlueXML.toc.sig`. It must be exactly 0x114 = 276 bytes. *(verified: disassembly @0x008165ff..0x00816633; every archive that has one stores a 276-byte `GLUEXML.TOC.SIG`)*
- **Layout of the .sig:** `[0:16]` = MD5 of the whole GlueXML file tree, `[16:20]` = `"NGIS"`, `[20:276]` = a 256-byte RSA signature. *(verified: I recomputed the MD5 with the reader; it matches the .sig in `patch-enUS-3.MPQ` byte for byte, `b44247235136edcfbbc314797eb2c705`. FrameXML matches too: `51b264400410d3409c4e79f6f4257711`.)*
- **Embedded public key:** a 256-byte RSA-2048 modulus @0x009e2c28 (the next string, `Fonts\NIM_____.TTF`, starts exactly 256 bytes later) and the exponent `01 00 01` (65537) @0x00a4421c, both passed to the verifier. *(verified: byte layout; inferred: that 0x00770db0 is the RSA verify primitive)*
- **What the MD5 covers:** the .toc file, then every file it lists (lines starting with `#` are skipped), recursively, plus every `<Script file=…>` and `<Include file=…>` in each .xml, in load order. Each file is resolved through the normal MPQ chain, so the highest-priority patch copy wins. *(verified: string compares with `.toc` @0x00a1da20, `.xml` @0x00a44220, `Script` @0x00a43d60 and `Include` @0x00a43d94; and my reimplementation over 60 files reproduces the signed digest exactly, using the copy from the highest-priority archive.)* So **every GlueXML Lua and XML file is covered**. Textures, models, sounds and DBCs that the code merely names are **not** hashed.
- **Signed message:** `.sig[0:20]` plus the toc's basename, checked against the last 256 bytes. *(inferred from FUN_00816550's buffer assembly; I did not attempt the verify math myself.)* Without Blizzard's private key, nobody can produce a .sig for a modified tree.

### Functions (names are mine)
| Address | Name | Role |
|---|---|---|
| 0x004da5f0 | `GlueMgr_LoadGlueXML` | Opens `Logs\GlueXML.log`, verifies, then loads `Interface\GlueXML\GlueXML.toc` |
| 0x008165e0 | `XML_VerifyTocSignature(toc, extraFile, rsaKey, outMd5)` | Returns 0 = missing .sig, 1 = corrupt signature, 2 = modified/corrupt, 3 = OK |
| 0x00816550 | `Sig_RsaVerify` | RSA check of the .sig header plus the basename against the key @0x009e2c28 |
| 0x008164d0 | `XML_HashTree` | MD5 init, then hash the toc tree, then optionally the extra file (FrameXML: `Bindings.xml`), then finalize |
| 0x008162b0 | `XML_HashFile` (recursive) | Loads a file through 0x00424e80, MD5-updates it, recurses for .toc/.xml |
| 0x00779340 / 0x00779a30 / 0x00779ae0 | MD5 init / update / final | Constants 0x67452301… *(verified)* |
| 0x00814340 | `XML_LoadToc` | Actually loads the UI while feeding a second MD5 context |
| 0x004033c0 | `App_Quit(code)` | Stores the code in 0x00b2f9a4 and posts quit (the glue Lua `QuitGame` stub 0x004d8350 calls it with 0) |

Pseudocode of the glue path (from the listing at 0x004da7c2..0x004da8eb):
```
md5_init(&live)
switch (XML_VerifyTocSignature("Interface\\GlueXML\\GlueXML.toc", NULL, RSA_KEY, &signedMd5)) {
  case 0: log("GlueXML missing signature");       goto quit;   // 0x004da7f1
  case 1: log("GlueXML has corrupt signature");   goto quit;   // 0x004da7fe
  case 2: log("GlueXML is modified or corrupt");  goto quit;   // 0x004da80b
  case 3: break;                                                // 0x004da835
}
XML_LoadToc("Interface\\GlueXML\\GlueXML.toc", 0, &live, &errlog) // real load, hashing again
md5_final(&liveMd5, &live)
if (memcmp(liveMd5, signedMd5, 16) != 0) goto quit              // silent second check
register glue Lua functions …; return
quit: App_Quit(9); errlog dtor writes entries to Logs\GlueXML.log
```
Inside `XML_VerifyTocSignature`: load `.sig` (missing → 0); size ≠ 0x114 → 1; RSA fails → 1; `XML_HashTree(toc) == sig[0:16]` → 3, else → 2.

### When
- At every GlueXML (re)load, not per file read. `GlueMgr_LoadGlueXML` has four callers: 0x00406696 (FUN_00406510), 0x004daaa5 (FUN_004da9d0), 0x004dac1c (FUN_004dab40) and 0x004dbb0d (FUN_004db9f0). *(verified: xrefs. inferred: that these are startup and the returns to the glue screens.)*
- The whole tree is hashed **before** any GlueXML code runs, and hashed again during the real load. *(verified: call order)*

### Later-loaded patch MPQs, and what happens on failure
- Files from a later patch MPQ **are** checked, because hashing uses the same file loader and the same priority chain as the real load. Proof: Blizzard's own `patch-enUS-3.MPQ` ships a new `GLUEXML.TOC.SIG` but no new toc, and the digest only matches when the -3 copies of the changed files are used. *(verified by recomputation)* The exe accepts `patch-4.MPQ`, `patch-%s-4.MPQ`, `patch-?.MPQ` and `patch-%s-?.MPQ` (strings @0x009e26d8..0x009e2710). *(verified: strings. inferred: their priority over -3.)*
- On failure there is **no fallback to the stock UI**. The message goes into an error list whose destructor (0x004da2d0) writes it to `Logs\GlueXML.log` (handle opened @0x004da7a0), and `App_Quit(9)` ends the client. *(verified: control flow. inferred: that there is no modal dialog. Phase B should check for one.)*

### FrameXML, for comparison
FUN_0052a980 does the same thing with `Interface\FrameXML\FrameXML.toc` @0x00a02fcc, adding `Interface\FrameXML\Bindings.xml` @0x00a02fec to the hash, and quits with code **10**. The messages are @0x00a02f70/90/b0, and the second, silent MD5 check is at 0x0052acaa..0x0052ad47. A third caller of the verifier, FUN_005f79a0 (`.\AddOns.cpp`, `Interface\AddOns\%s\%s.toc`), applies it to AddOns, presumably to mark Blizzard-signed addons as secure. *(verified: xrefs and strings. inferred: its purpose.)*

**Confidence:** high that any edit to a GlueXML .lua/.xml/.toc in a patch MPQ makes the client quit (the digest recomputation leaves little room for doubt). Medium on the exact failure UX (log only, or log plus dialog) until Phase B.

## Q2: backdrops and music still in the 3.3.5a MPQs

All three login backdrops ship in WotLK M2 format (`MD20`, version 264). The "effective" copy is the one in the highest-priority archive (patch-enUS-x > patch-enUS > locale-enUS). That order matches the Q1 digest recomputation.

| Era | Asset | Archive | Internal path | Size (bytes) |
|---|---|---|---|---|
| WotLK | model (effective) | `Data/enUS/patch-enUS-2.MPQ` | `Interface\GLUES\MODELS\UI_MAINMENU_NORTHREND\UI_MainMenu_Northrend.m2` (+`00..03.skin`, 175488/175488/175488/176048) | 1358144 |
| WotLK | model (older) | `Data/enUS/patch-enUS.MPQ` / `locale-enUS.MPQ` | `…\UI_MainMenu_Northrend\UI_MainMenu_Northrend.M2` | 1322144 / 1323888 |
| TBC | model (effective) | `Data/enUS/patch-enUS.MPQ` | `Interface\GLUES\MODELS\UI_MainMenu_BurningCrusade\UI_MainMenu_BurningCrusade.m2` (+`00..03.skin`, 217984×3 / 218048) | 1069056 |
| TBC | model (older) | `Data/enUS/expansion-locale-enUS.MPQ` | same path | 1062144 |
| Original (Dark Portal) | model | `Data/enUS/locale-enUS.MPQ` | `Interface\GLUES\MODELS\UI_MAINMENU\UI_MainMenu.m2` (+`00.skin` 110288, `01.skin` 110368; 69 files in the folder) | 438128 |
| WotLK | music | `Data/common.MPQ` | `Sound\Music\GlueScreenMusic\WotLK_main_title.mp3` | 10883817 |
| TBC | music | `Data/expansion.MPQ` | `Sound\Music\GlueScreenMusic\BC_main_theme.mp3` | 4534893 |
| Original | music | `Data/common.MPQ` | `Sound\Music\GlueScreenMusic\wow_main_theme.mp3` | 3225984 |
| (all) | login ambience | `Data/common.MPQ` | `Sound\Ambience\GlueScreen\GlueScreenLogin.wav` | 5380244 |

Music is chosen by sound-kit name, not by file. `DBFilesClient\SoundEntries.dbc` (effective copy in `patch-enUS-3.MPQ`, 2405359 bytes) maps: 10924 `GS_Retail` → `wow_main_theme.mp3`, 10925 `GS_BurningCrusade` → `BC_main_theme.mp3`, 12765 `GS_LichKing` → `WotLK_main_title.mp3`, all under `Sound\Music\GlueScreenMusic`; and 9902 `GlueScreenIntro` → `Sound\Ambience\GlueScreen\GlueScreenLogin.wav`. *(verified: parsed DBC records)*

Where GlueXML picks the backdrop and music (effective files from `patch-enUS-3.MPQ`; the toc comes from `patch-enUS-2.MPQ`):
- `Interface\GlueXML\AccountLogin.lua` (22891 bytes), `AccountLogin_OnLoad`, lines 32–37:
  ```lua
  if (IsStreamingTrial()) then
      AccountLoginCinematicsButton:Disable();
      AccountLogin:SetModel("Interface\\Glues\\Models\\UI_MainMenu\\UI_MainMenu.m2");
  else
      AccountLogin:SetModel("Interface\\Glues\\Models\\UI_MainMenu_Northrend\\UI_MainMenu_Northrend.m2");
  end
  ```
  and `AccountLogin_OnShow`, lines 42–43: `PlayGlueMusic(CurrentGlueMusic); PlayGlueAmbience(GlueAmbienceTracks["DARKPORTAL"], 4.0);`
- `Interface\GlueXML\GlueParent.lua` (16897 bytes), line 2: `CurrentGlueMusic = "GS_LichKing";` and line 46: `GlueAmbienceTracks["DARKPORTAL"] = "GlueScreenIntro";`. The TBC model is not referenced by the stock 3.3.5a GlueXML at all.

**Implication (inferred, to test in Phase B):** an in-client picker needs a GlueXML change, which Q1 rules out without an exe change. But because models, sounds and DBCs are not hashed, one backdrop and track per launch could be swapped **data-only**. A patch MPQ would place the chosen model (plus skins) at the Northrend path and/or change the `GS_LichKing` row of `SoundEntries.dbc`, and the launcher would pick which patch MPQ to install before starting the client.

## Phase B plan (proposed; recommendation left to the orchestrator)

Setup, so the real `client/` is never written:
- Make a scratch client with an APFS clone: `cp -cR client/ env/spike-client/`. This is copy-on-write, so it uses almost no extra space, and `client.inspect`/`re.mpq_readonly` keep proving `client/` unchanged. `hs launch`/`start_client` currently hard-code `P.CLIENT`, so Phase B needs a client-dir override (an env var or flag). The alternative, adding the MPQ to `client/Data` and removing it afterwards with `client.inspect` proving restoration, is the fallback.
- Build the test archives with an MPQ **writer**, which A.1 deliberately doesn't have. Options: pinned StormLib (Homebrew `stormlib` 9.40, bottled) through a small ctypes helper, or a minimal stdlib writer (uncompressed files plus encrypted hash/block tables, reusing `mpq.py`'s crypto). Either way, read the archive back with `hs mpq list` before using it.
- Name: `Data/enUS/patch-enUS-4.MPQ` (a pattern the exe accepts; see Q1).

Runs, each a fresh launch to the login screen with a screenshot plus `Logs/GlueXML.log`, the process exit status, and the Wine log:
1. **Control:** the scratch copy without the extra MPQ boots to the normal Northrend login screen with an empty or clean `GlueXML.log`.
2. **GlueXML edit (Q1 decisive):** `patch-enUS-4.MPQ` holds only `Interface\GlueXML\GlueStrings.lua` with one visible change (e.g. the login button label string). Prediction from RE: the client exits (code 9) and `Logs\GlueXML.log` contains "GlueXML is modified or corrupt", with no login screen. **This disproves the data-only route** for the front menu. If the login screen instead appears with the changed label, the RE is wrong and the data-only route is open.
3. **Assets only (picker feasibility):** `patch-enUS-4.MPQ` holds the Dark Portal model saved under the Northrend path (`…\UI_MainMenu_Northrend\UI_MainMenu_Northrend.m2` + `00.skin`, `01.skin`; also `02/03.skin` copied from `01` in case the client asks for higher LODs, along with the textures it references) and a `SoundEntries.dbc` whose `GS_LichKing` row points at `wow_main_theme.mp3`. Prediction: no GlueXML error, Dark Portal backdrop plus the original theme. **This proves a launcher-side, data-only background/music choice.**

What would settle each question:
- Q1 data-only front menu: run 2 shows the edit → feasible; run 2 quits with the logged message → not feasible without an exe change (that would need an ADR and Zach's yes).
- Q2 background picker: run 3 shows the swapped backdrop/music → feasible as a pre-launch launcher choice (not inside the login screen); run 3 fails → only via an exe change.

## Open questions
- Does the failure show a modal dialog under Wine, or only log and quit? (Phase B run 2.)
- `hs re decompile` doesn't work on Apple Silicon with the pinned Ghidra (no `mac_arm_64` decompiler native). Should we build the natives (needs a toolchain; changes `env/ghidra_*`), or add a disassembly mode to `HsReport.java`?
- MPQ reader coverage: implode (PKWARE) and per-file encryption paths are implemented but none of the 656 files sampled across all 18 archives used them, so they are untested on real data. Huffman/ADPCM (WAV-only) are not implemented.
