# Tasks

## 1. Play client (builder)
- [ ] 1.1 Pin patch 0001 bytes from disassembly of a copy; ADR-0009 appendix — verify: `client.patch0001_recorded`
- [ ] 1.2 `hs client-build`: clone + queue + stamp + WTF carry-over; `client = patched|stock` wiring in ./play and the app — verify: `client.play_build`, `client.play_revert`, `client.stock_mode`

## 2. Login screen (builder)
- [ ] 2.1 GlueXML: profile list + auto-login with cancel; generated credentials file — verify: `login.auto_login`, `login.cancel_shows_list`, `login.no_glue_errors`, `login.continue_context`
- [ ] 2.2 Backdrop picker (WotLK/TBC/Vanilla + music), remembered — verify: `login.backdrop_choice`

## 3. Verify + human
- [ ] 3.1 Verifier pass + full harness
- [ ] 3.2 Human: Zach launches the .app and lands in game without typing; tries the backdrop picker
