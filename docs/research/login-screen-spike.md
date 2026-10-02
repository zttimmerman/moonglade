# Login-screen spike: findings and recommendation

Change: `openspec/changes/login-screen-spike/`. Status: **Phase A (read-only analysis) and Phase B (controlled launches on a scratch copy of the client) done.** Jump to [Recommendation for Zach](#recommendation-for-zach) for the plain-language summary.

Provenance: `Wow.exe` 3.3.5a (build 12340), SHA-256 `aa63a5750d60ef16746c686b3d5e26876d98953eab08b1c026cd0faf78e88cb8`, analyzed as a copy in `$HS_RE_DIR/bin` (ADR-0005). MPQ facts come from `harness/hs mpq` (stdlib reader, `harness/hslib/mpq.py`) on the installed client, read-only. Labels: **verified** means checked against bytes, code or a recomputed hash; **inferred** means read from disassembly or naming, not confirmed at runtime.

Tooling note: the pinned Ghidra 12.1.4 ships no `decompile` native for `mac_arm_64` (only `linux_x86_64` and `win_x86_64`), so `hs re decompile` returns an error on this host. All the code reading below comes from Ghidra's **disassembly** listing, run through a scratch read-only script against the analyzed project (`-readOnly`, never saved). The pseudocode is hand-written from that listing.

## Q1: can a patch MPQ change GlueXML with an unmodified Wow.exe?

**Short answer: no, not for anything GlueXML loads as code. Assets that GlueXML only references by name (models, textures, sounds, DBCs) are not covered by the check.** Phase B confirmed both halves at runtime (runs 2 and 3 below).

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
| 0x004033c0 | `App_Quit(code)` | Stores the code in 0x00b2f9a4 and posts quit (the glue Lua `QuitGame` stub 0x004d8350 calls it with 0). Phase B: the 9 is **not** the process exit status; Wow.exe exits with 0 |

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
- Files from a later patch MPQ **are** checked, because hashing uses the same file loader and the same priority chain as the real load. Proof: Blizzard's own `patch-enUS-3.MPQ` ships a new `GLUEXML.TOC.SIG` but no new toc, and the digest only matches when the -3 copies of the changed files are used. *(verified by recomputation)* The exe accepts `patch-4.MPQ`, `patch-%s-4.MPQ`, `patch-?.MPQ` and `patch-%s-?.MPQ` (strings @0x009e26d8..0x009e2710). *(verified: strings; and at runtime in Phase B: `patch-enUS-4.MPQ`, `patch-enUS-H.MPQ` and `Data/patch-4.MPQ` all load and override files from `patch-enUS-3.MPQ` / `patch-enUS-2.MPQ`.)*
- On failure there is **no fallback to the stock UI**. The message goes into an error list whose destructor (0x004da2d0) writes it to `Logs\GlueXML.log` (handle opened @0x004da7a0), and `App_Quit(9)` ends the client. *(verified: control flow; and at runtime in Phase B run 2: no dialog, the log line, process exit code 0.)*

### FrameXML, for comparison
FUN_0052a980 does the same thing with `Interface\FrameXML\FrameXML.toc` @0x00a02fcc, adding `Interface\FrameXML\Bindings.xml` @0x00a02fec to the hash, and quits with code **10**. The messages are @0x00a02f70/90/b0, and the second, silent MD5 check is at 0x0052acaa..0x0052ad47. A third caller of the verifier, FUN_005f79a0 (`.\AddOns.cpp`, `Interface\AddOns\%s\%s.toc`), applies it to AddOns, presumably to mark Blizzard-signed addons as secure. *(verified: xrefs and strings. inferred: its purpose.)*

**Confidence:** high, now confirmed at runtime (Phase B run 2): a one-line edit to a GlueXML .lua in a patch MPQ makes the client quit, with no dialog.

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

**Implication (confirmed in Phase B run 3):** an in-client picker needs a GlueXML change, which Q1 rules out without an exe change. But because models, sounds and DBCs are not hashed, one backdrop and track per launch could be swapped **data-only**. A patch MPQ would place the chosen model (plus skins) at the Northrend path and/or change the `GS_LichKing` row of `SoundEntries.dbc`, and the launcher would pick which patch MPQ to install before starting the client.


## Phase B: controlled launches (2026-10-02)

### Setup
- **Scratch client:** `cp -cR client/ env/spike-client/` (APFS clone, about zero extra disk). Every test archive went only into `env/spike-client/Data/...`. The real `client/` stayed untouched: `harness/hs run --only client.inspect` PASSed before and after, and a size+mtime listing of all 237 files under `client/` was identical before and after (`env/spike-b/client-snapshot-{before,after}.txt`). `Wow.exe` and every DLL in the scratch copy matched `env/client-manifest.json` before and after every run (`binaries_match_manifest_before/after: true` in each `result.json`).
- **Launch path:** a new test-only override, `HS_CLIENT_DIR`, in `harness/hslib/launch.py` (`launch.client_dir()`; default = the real `client/`). It only changes which directory `hs launch` configures and starts. `client.inspect`, `re.*` and the manifest still describe the real `client/`, and the scratch binaries must still match that manifest.
- **Test archives:** a new minimal stdlib MPQ writer, `harness/hslib/mpq.py` `write_archive()` / `hs mpq write` (format v1, 4 KiB sectors, zlib or stored, standard-key encrypted hash/block tables, a `(listfile)`). Every archive was read back byte-for-byte by the existing reader before use. New quick-tier check `re.mpq_write_roundtrip` (spec mapping left to the orchestrator).
- **Driver and evidence:** a scratch script (`env/spike-b/drive.py`, not committed) ran `launch.prepare()` + `start_client()` with `HS_CLIENT_DIR=env/spike-client`, polled the Wine process list and on-screen windows once a second, then stopped the prefix (`wineserver -k`). Per run it saved `timeline.json`, `result.json` and the run's `wine.log` slice under `env/spike-b/<run>/`.
- **Screenshots:** macOS `screencapture` is refused in this session ("could not create image from window"; no Screen Recording permission). Instead, a 2.5 KB helper (`env/spike-b/tools/k.c`, built with clang + lld-link, run inside the same Wine prefix) sends Print Screen to the WoW window, so **the client takes its own screenshot** (`Screenshots/WoWScrnShot_*.jpg`). A second helper (`tools/x.c`) runs Wow.exe and logs its real exit code, because the Wine launcher always returns 0.
- Nothing typed into the GUI. The login screen is the evidence; no login with any account. Stack and Wine left down at the end.

### Results
| Run | Patch archive in the scratch client | What happened | Evidence |
|---|---|---|---|
| 1 control | none | Normal WotLK login screen (Arthas/Frostwyrm backdrop). Window up from t≈11 s to the end (45 s); no `GlueXML.log` written | `env/spike-b/run1-control/`; [screenshot](img/login-spike-run1-control.jpg) |
| **2 Q1** | `Data/enUS/patch-enUS-4.MPQ` holding only `Interface\GlueXML\GlueStrings.lua` with one line changed (`LOGIN = "Login";` → `LOGIN = "HS Login";`) | **Client quits by itself.** Wow.exe started, a blank 1024×800 game window appeared at t≈6 s and was gone at t≈9.4 s. **No dialog, no crash reporter, no `Errors/` file.** `Logs\GlueXML.log`: `10/2 19:34:09.722  GlueXML is modified or corrupt`. Process exit code **0** (repeated under the exit-code helper: `[hs-exitcode] Wow.exe exit code 0`, same log line) | `env/spike-b/run2-gluexml-mod/`, `run2-gluexml-mod-exitcode/`, `run2-gluexml-mod-trace/` |
| 2b control | same archive name, but `GlueStrings.lua` byte-identical to Blizzard's | Normal login screen. So the quit in run 2 is caused by the **changed content**, not by the extra archive or the writer | `env/spike-b/run2b-gluexml-identical/` |
| **3 picker** | `patch-enUS-4.MPQ` holding the original Dark Portal model (`UI_MainMenu.m2` + `00/01.skin` from `locale-enUS.MPQ`) saved under the Northrend path (`…\UI_MainMenu_Northrend\UI_MainMenu_Northrend.m2`, `00–03.skin`), plus `SoundEntries.dbc` with the `GS_LichKing` row (12765) repointed from `WotLK_main_title.mp3` to `wow_main_theme.mp3` (a 4-byte change: the string offset GS_Retail already uses) | **Dark Portal login screen, no error**, no `GlueXML.log`. Login box, buttons and the WotLK logo unchanged (the logo is a separate texture) | `env/spike-b/run3-darkportal/`; [screenshot](img/login-spike-run3-darkportal.jpg) |
| **3b music only** | `patch-enUS-4.MPQ` holding **only** run 3's edited `SoundEntries.dbc` (GS_LichKing → `wow_main_theme.mp3`; nothing else) | **Normal WotLK login screen, no quit, no `GlueXML.log`, no error.** So the DBC edit is outside GlueXML's signed set and the signature check is unaffected. `Logs/SESound.log` names no tracks (it only reports FMOD init and volume settings), and the Wine `+file` trace can't name the track: the stock songs are read from inside `common.MPQ` through one handle, and `NtReadFile` traces don't print offsets. The next row closes that gap | `env/spike-b/run3b-music-only/` (screenshots, `wine.log` with `+file`) |
| **3b probe** | like 3b, but the GS_LichKing row's `DirectoryBase` also points at a new string, `Interface\AddOns\HSMusicProbe`, and a loose copy of `wow_main_theme.mp3` (extracted from `common.MPQ`, byte-identical, 3225984 bytes) sits in that folder **of the scratch client** | Normal login screen, no error. The Wine `+file` trace **names the track**: `CreateFileW L"Z:\…\spike-client\Interface\AddOns\HSMusicProbe\wow_main_theme.mp3"` (handle 0x2250, opened once). Audio threads 0168/0174 then read it 27 times (385,024 bytes, about 20 s of a 128 kbps MP3) while the login screen was up. So **the login screen's music comes from the GS_LichKing row, and with our edit it streams the classic theme.** Actual sound output is not captured | `env/spike-b/run3b-probe-music/wine.log` (lines ~18859–20824) |
| 3e | `patch-enUS-4.MPQ` holding only a `SoundEntries.dbc` with a deliberately broken header (29 columns instead of 30) | Client stops with `ERROR #121 (0x85100079) Version Mismatch: DBFilesClient\SoundEntries.dbc has wrong number of columns (found 29, expected 30)` (WowError dialog). This proves the client **reads `SoundEntries.dbc` from our patch archive**, and checks only its shape, not its content | `env/spike-b/run3e-bad-dbc/Error.txt` |
| 3c | run 3's archive as `Data/patch-4.MPQ` | Dark Portal login screen | `env/spike-b/run3c-name-patch-4/` |
| 3d | run 3's archive as `Data/enUS/patch-enUS-H.MPQ` | Dark Portal login screen | `env/spike-b/run3d-name-patch-enUS-H/` |

### What this settles
- **Q1 (custom login-screen UI with the stock Wow.exe): not possible.** Phase A's prediction holds: a one-line GlueXML edit in a patch MPQ makes the client close itself about 3 seconds after its window appears, with only a line in `Logs\GlueXML.log`. To the player it looks like the game opening and silently closing. Correction to Phase A: the "9" passed to `App_Quit` is internal; the process exit status is 0.
- **Q2 (backdrop/music picker): feasible data-only, one choice per launch.** A patch MPQ of renamed models and an edited DBC row changes the login backdrop with no error (verified by the client's own screenshot). **The music swap works on its own** (run 3b): the edited `SoundEntries.dbc` alone gives a normal login screen with no GlueXML complaint. The 3b probe proves *which* track: with the row pointed at a loose copy, the client opens and streams `wow_main_theme.mp3` at the login screen. Run 3e proves the client takes `SoundEntries.dbc` from our patch archive. Only whether the sound actually reaches the speakers is unmeasured (no audio capture without new permissions): *[human-verify]*, steps under Open questions.
- **Archive names the client accepts (runtime-verified):** `Data/enUS/patch-enUS-4.MPQ`, `Data/enUS/patch-enUS-H.MPQ` (a letter works) and `Data/patch-4.MPQ`. All of them override files from the stock `patch-enUS-2/-3.MPQ`.

## Recommendation for Zach

**(a) Background and music picker: yes, without touching the game program.** The launcher can offer "WotLK / Burning Crusade / Original" for the login screen. Before it starts the game, it drops one small add-on file (under 1 MB for the Dark Portal) into the game's Data folder, or removes it for the default look. The test showed the classic Dark Portal backdrop with no errors. The music swap was tested on its own: the game reads and plays the original theme file at the login screen with no errors. Only hearing it is left (a 1-minute listen). Limits:
- The choice is made **in the launcher, before the game opens**, not on the login screen itself. Changing it means restarting the game.
- Only the backdrop and music change. The WotLK logo, the buttons and the text stay as they are. The logo is probably swappable the same way (it's a texture, not code), but that's untested.
- Burning Crusade should work the same way (its model is in the files, under another name). It wasn't tested separately.

**(b) A real front menu inside the login screen (profiles, saves, options, auto-login) needs a change to Wow.exe.** The login screen's layout and logic (GlueXML) are signed by Blizzard. If even one word changes, the game quits silently (run 2). There's no way around this with data files. The smallest change would be to the game program's own check, two decision points in one function, using the addresses from Phase A:
1. In `GlueMgr_LoadGlueXML` (0x004da5f0), after the signature check `XML_VerifyTocSignature` (0x008165e0): treat "modified" (result 2) as "OK" (result 3). The branch is in the listing at 0x004da7c2..0x004da835.
2. In the same function, after the real load (`XML_LoadToc`): skip the second, silent MD5 comparison of the loaded files against the signed digest (the compare-and-quit after case 3, inside the listing range 0x004da835..0x004da8eb). Without this, step 1 alone would still quit.

The exact bytes still need a read-only RE pass; none are proposed here. FrameXML (the in-game UI, FUN_0052a980) has its own copy of the check, but the front menu doesn't need it.

Risks and costs:
- It breaks the fixed rule "no client binary patching in v1" (CLAUDE.md §2), so it needs **your explicit yes plus an ADR** before anyone writes a byte. This spike doesn't do it.
- We would ship a patched copy (e.g. `Wow-hs.exe`) next to the pristine `Wow.exe`, never overwrite it. `client.inspect` and the manifest would have to record the patched copy's hash.
- A wrong byte means crashes at startup or on returning to the login screen. That's easy to detect with the existing launch smoke test, and easy to undo by using the original exe.
- Offline there's no anti-cheat consequence: Warden is already disabled on our server. The signature check only protects Blizzard's UI integrity.
- After the patch, the front menu itself is ordinary Lua/XML work in a patch MPQ, which is the approach the original UI uses.

**Alternative if you'd rather not patch the program:** put profiles/saves/options in the macOS launcher and use the login screen as-is, with the data-only backdrop picker. You'd lose an in-game front menu and auto-login inside the client.

**Decisions for you:** (1) build the launcher-side backdrop/music picker (no risk, no program change)? (2) open the Wow.exe signature-check patch (ADR + your approval) for the in-client front menu, or keep the menu in the launcher?

## Open questions
- **Music by ear** *[human-verify]* (the trace proves the client streams `wow_main_theme.mp3`; nothing here captures speaker output). Steps for Zach:
  1. `cp -cR client/ env/spike-client/`
  2. `harness/hs mpq write env/spike-client/Data/enUS/patch-enUS-4.MPQ 'DBFilesClient\SoundEntries.dbc=env/spike-b/files/SoundEntries.mod.dbc'`
  3. `HS_CLIENT_DIR=env/spike-client harness/hs launch`, wait for the login screen, and listen. Expected: the original 2004 WoW theme (choir and orchestra), not the WotLK title music. For comparison, plain `harness/hs launch` on the real client plays the WotLK theme. Music volume is 0.40 in Config.
  4. Quit the game, then `rm -rf env/spike-client`.
- **Logo and TBC backdrop:** swapping the WotLK logo texture and the TBC model the same way is expected to work, but neither was tested.
- **Screenshots in the harness:** this session has no macOS Screen Recording permission. The in-Wine Print Screen helper works and could become a harness feature (it needs a Windows cross-compile step, clang + Homebrew `lld-link`, or a prebuilt helper with a pinned hash). It's not added here.
- **System stall during the first attempt:** the first run-1 attempt (17:54) started while the display was off. The driver then hung, and MySQL aborted with "Semaphore wait has lasted > 600 seconds" (`env/logs/mysqld.err`, 22:10Z). All later runs used `caffeinate -d -i -u` and behaved. The cause isn't proven; if unattended launches with the display off matter, it's worth a look.
- `hs re decompile` doesn't work on Apple Silicon with the pinned Ghidra (no `mac_arm_64` decompiler native). Should we build the natives, or add a disassembly mode to `HsReport.java`? Relevant if the exe-patch ADR is opened.
- MPQ reader coverage: the implode (PKWARE) and per-file encryption paths are implemented but untested on real data (none of the 656 sampled files used them). Huffman/ADPCM (WAV-only) are not implemented. The writer is intentionally minimal: neutral locale only, no `(attributes)`, no per-file encryption.
