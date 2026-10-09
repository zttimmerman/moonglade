## 1. Mode setting and packaging steps
- [ ] 1.1 `tools/release/signing.json` (`{"mode": "ad-hoc"}`), `tools/release/sign.py` (mode reading, credential presence check listing missing names, refusal of `developer-id` outside CI, `--signing-config` limited to `env/release/` and `harness/out/`); `release.py` steps `sign` and `notarize` in `ALL` (design 1)
- [ ] 1.2 Final bundle id `io.github.zttimmerman.moonglade` in `release.py`; nested identifiers `<bundle id>.<file name>`; record Oracle files' cdhashes at `payload`; update the docstring layout and `release-first-launch`'s references that name `local.moonglade.release` in code/checks (list each edit; none loosens)
- [ ] 1.3 `.github/workflows/release.yml`: mode step from `signing.json`; sign before the smoke step and point the smoke at the signed app; `developer-id` steps (secret check, temp keychain + import, `notarize`, `spctl` on a fresh unzip, `always()` keychain/file cleanup); notes from `tools/release/notes/<mode>.md`

## 2. Signing
- [ ] 2.1 Ad-hoc inside-out signing: every non-Oracle Mach-O with `--options runtime` and its role's entitlements, the launcher, then the bundle with the pinned designated requirement; per-file and bundle verification; `env/release/build/sign.json` record (design 1, 3, 4)
- [ ] 2.2 Entitlements probe: sign with none, run the release smoke, `firstlaunch.flow`, `release.play_standin`, `release.client_smoke` from the signed fixture; add only the keys a failure proves, each with its evidence line in design 3; write `tools/release/entitlements/*.plist` (XML)
- [ ] 2.3 Developer ID path in `sign.py`: identity from the keychain, `--timestamp`, default designated requirement; `notarize` (submit `--wait` with the API key, save the log, fail unless `Accepted` with no issues, staple, validate, re-zip, refresh `SHA256SUMS`/`SOURCES.json`). Exercised locally only up to the credential check

## 3. Icon and Info.plist
- [x] 3.1 Get the approved navy and logo scale from Zach's mockup (ask the orchestrator; else propose values) and the template's corner radius (record the source or flag it); `icon.py` tile stage with `AppIcon.json` `tile` record; regenerate `branding/AppIcon.{png,icns,json}`; preview sheet (16-1024 px, light and dark) for Zach
- [ ] 3.2 Shim test-mode icon sampling (`HSDOCK_ICON_SAMPLES` → logged RGBA); `launch.dock_env()` unchanged unless a check shows a path not using `branding/`
- [ ] 3.3 Drop the dev plist's `NSHumanReadableCopyright` line; rebuild the dev app once (note in STATUS whether macOS asked for any permission again)

## 4. Checks and docs
- [ ] 4.1 `signing.mode_setting`, `signing.bundle_layout`, `signing.adhoc_signature`, `signing.entitlements_needed`, `signing.gatekeeper_adhoc`, `signing.stable_identity`, `signing.seal_survives_use`, `signing.open_instructions`, `signing.notarized` (CI-only; not applicable locally), each with a counterexample (the notarized one: a record with an issue in the notary log fails it)
- [ ] 4.2 `app.icon` rewritten for the tile (counterexamples: old transparent layout, white tile), `app.bundle_valid` tightened (plist wording), `app.icon_everywhere`, `app.dock_icon_rendered` (counterexample: transparent-surround `HSDOCK_ICON`)
- [ ] 4.3 `tools/release/notes/{ad-hoc,developer-id}.md`; `docs/setup.md` "Using a release" → "Opening it the first time" (both modes, the one-liner, "once per version", what ad hoc can't do in plain words); design.md "As built"
