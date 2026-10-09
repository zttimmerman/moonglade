# release-signing Specification

## Purpose
TBD - created by archiving change release-signing. Update Purpose after archive.

## Requirements

### Requirement: One checked-in setting selects the signing mode
`tools/release/signing.json` SHALL hold the signing mode, `ad-hoc` (the default) or `developer-id`, as its one setting, so switching modes is a one-line change. Packaging and the release workflow SHALL read the mode only from that file (a scratch copy given by a packaging flag is allowed for checks). An unknown or missing mode SHALL stop packaging with a message naming the file. `ad-hoc` SHALL never need or read any secret. `developer-id` SHALL check, before building or uploading anything, that every credential it needs is present, and SHALL fail naming each missing one (names only, never values).

#### Scenario: Ad-hoc signs with no secrets
- **GIVEN** a small fixture app shaped like the release app (launcher, a payload with one executable and one dylib) and an environment with no signing or notary variables at all
- **WHEN** the sign step runs with the checked-in `ad-hoc` setting
- **THEN** it succeeds, the record names mode `ad-hoc`, and no secret name was looked up (the step's record lists the environment keys it read: none of the Developer ID ones)
- Check: `signing.mode_setting`

#### Scenario: Developer ID without secrets fails first
- **GIVEN** the same fixture and a scratch setting file with `developer-id`, and none of the Developer ID credentials in the environment
- **WHEN** the sign step runs
- **THEN** it exits nonzero before signing anything, the message names every missing credential, the fixture app's files are byte-identical afterwards, and no zip exists
- Check: `signing.mode_setting`

#### Scenario: Unknown mode refused
- **GIVEN** a scratch setting file with mode `adhoc-typo`, and one with the file missing
- **WHEN** the sign step runs
- **THEN** each exits nonzero naming the file and the allowed modes, and nothing is signed
- Check: `signing.mode_setting`

#### Scenario: The workflow follows the setting
- **GIVEN** `.github/workflows/release.yml` and the checked-in setting
- **WHEN** the workflow file is parsed
- **THEN** the mode comes from `tools/release/signing.json`; the sign step runs before the no-Homebrew smoke step and the smoke runs on the signed app; every step that references a secret is conditional on `developer-id`; the `developer-id` path checks the secrets, imports the certificate into a temporary keychain that is deleted in an `always()` step, notarizes, staples and re-zips before the draft is created; and the draft's notes come from the active mode's notes file
- Check: `signing.mode_setting`

### Requirement: Release app identity and layout
The release app's bundle identifier SHALL be `io.github.zttimmerman.moonglade`, never the dev app's `local.moonglade.app`. Its executable SHALL be `Contents/MacOS/Moonglade`; the payload SHALL stay at `Contents/Resources/Moonglade/`; every Mach-O file in the bundle SHALL be inside `Contents/MacOS/` or the payload's known code folders (`bin/`, `server/bin`, `server/lib`, `mysql/bin`, `mysql/lib`, `python/bin`, `python/lib`); and no code location (`Contents/MacOS`, `Contents/Frameworks`, `Contents/Helpers`, `Contents/PlugIns`) SHALL hold anything but Mach-O files. Nothing that runs SHALL write inside the bundle.

#### Scenario: Bundle id and layout of the packaged app
- **GIVEN** the packaged release app from this run's packaging
- **WHEN** its Info.plist and every file are inspected (`file` / Mach-O magic)
- **THEN** `CFBundleIdentifier` is `io.github.zttimmerman.moonglade`, `CFBundleExecutable` is `Moonglade` and exists under `Contents/MacOS`, every Mach-O is in an allowed place, `Contents/MacOS` holds only the launcher, and the Info.plist carries no "redistribution" wording; a planted Mach-O at `Contents/Resources/Moonglade/harness/x.dylib` in a scratch copy and a planted text file in `Contents/MacOS` each make the check fail
- Check: `signing.bundle_layout`

#### Scenario: The signature survives first launch and play
- **GIVEN** the shared first-launch fixture's app copy, after first launch, the stand-in play loop and a backup ran from it
- **WHEN** the bundle's signature is verified (`codesign --verify --deep --strict`) and each payload Mach-O is verified on its own
- **THEN** all pass and the bundle's file list and hashes equal those recorded at packaging (no `__pycache__`, no logs, nothing new)
- Check: `signing.seal_survives_use`

### Requirement: Ad-hoc signature
In `ad-hoc` mode the sign step SHALL sign, inside out: every Mach-O in the payload that isn't Oracle's (Oracle MySQL's binaries and libraries keep Oracle's Developer ID signature untouched), then the launcher, then the bundle. Every executable it signs SHALL carry the hardened runtime and exactly the entitlements in `tools/release/entitlements/<role>.plist` for its role (none when the role has no file); libraries SHALL carry no entitlements. The bundle's designated requirement SHALL be `identifier "io.github.zttimmerman.moonglade"`. Gatekeeper SHALL reject the ad-hoc app as expected, and nothing SHALL be presented as notarized.

#### Scenario: Every signature verifies
- **GIVEN** the packaged release app (ad-hoc mode)
- **WHEN** the check runs `codesign --verify --deep --strict` on the bundle, `codesign --verify --strict` on every Mach-O in it, and `codesign -d --entitlements - --xml` and `-dvvv` on each
- **THEN** all verify; each signed by us is `adhoc` with the `runtime` flag; each executable's entitlements equal its role's checked-in file exactly (and are an XML plist, not `bplist00`); libraries have none; Oracle's files still show Oracle's Developer ID authority and their original code-directory hash recorded at packaging; the bundle's designated requirement is the pinned one; and a scratch copy with one byte flipped in `server/lib/libzstd.1.dylib` fails the per-file verification
- Check: `signing.adhoc_signature`

#### Scenario: Every entitlement is needed
- **GIVEN** the checked-in entitlement files
- **WHEN** for each entitlement a scratch copy of the app is re-signed without it, and the flow its design entry names runs from that copy (release smoke, first-launch engine or stand-in play loop, as listed)
- **THEN** each such run fails in the named way, while the same flow passes with the full set; when no role has entitlements, the check instead asserts that no signed executable carries any
- Check: `signing.entitlements_needed`

#### Scenario: Gatekeeper rejects ad hoc as expected
- **GIVEN** a scratch copy of the packaged ad-hoc app with a `com.apple.quarantine` attribute set on it (never opened through LaunchServices, so no dialog appears)
- **WHEN** `spctl --assess --type execute -vv` assesses it
- **THEN** the assessment exits nonzero with "rejected", the output says no notarization or Developer ID source accepted it, and the check records the exact text
- Check: `signing.gatekeeper_adhoc`

#### Scenario: Grants survive an update
- **GIVEN** two ad-hoc release apps built from different commits (the second with a changed launcher byte and a different VERSION), so their code hashes differ
- **WHEN** each is tested against the first one's designated requirement (`codesign --verify -R=<requirement>`)
- **THEN** both satisfy it, the requirement names no code hash, and an app signed with another identifier does not satisfy it
- Check: `signing.stable_identity`

### Requirement: Developer ID signing and notarization in CI
In `developer-id` mode the release workflow SHALL import the Developer ID Application certificate from repo secrets into a temporary keychain; sign inside out like ad hoc but with that identity, the hardened runtime and a secure timestamp (`--timestamp`), and Apple's default designated requirement (team + identifier); submit the zip with `notarytool submit --wait` using an App Store Connect API key from repo secrets; fail unless the result is `Accepted` and the notary log lists no issues; staple the ticket to the app (`stapler staple`, `stapler validate`); zip the stapled app again with `ditto`; refresh `SHA256SUMS`; and delete the temporary keychain whatever happened. It SHALL never run outside the release workflow and never print a secret.

#### Scenario: Notarized, stapled release
- **GIVEN** a version tag pushed with `developer-id` set and all secrets present (release workflow, GitHub-hosted macOS runner)
- **WHEN** the workflow runs
- **THEN** the record shows every signed Mach-O with the team's Developer ID authority, a secure timestamp and the runtime flag; notarization `Accepted` with an empty issues list in the saved notary log; `stapler validate` passes; `spctl --assess --type execute -vv` on the unzipped final asset says `accepted` with source `Notarized Developer ID`; the no-Homebrew smoke passed on the signed app; and the zip in `SHA256SUMS` is the stapled one
- Check: `signing.notarized` (CI-only: runs as a workflow step in `developer-id` mode on the runner, reading that run's records; needs Zach's Apple credentials, which local runs never have. Locally it reports not applicable, never PASS.)

#### Scenario: A friend opens a notarized release
- **GIVEN** a published Developer ID release downloaded with Safari on another Mac
- **WHEN** it is unzipped and opened
- **THEN** macOS shows only the normal "downloaded from the internet" question and the app opens
- [human-verify] Needs a real paid Apple account, a real published release and a second Mac or user account; the Gatekeeper dialog can't be scripted without changing system security settings.

### Requirement: Plain-language "how to open it"
`docs/setup.md` ("Using a release") and the draft release's notes SHALL explain, in plain language, how to open the app the first time for the active mode. For `ad-hoc`: open it once, then System Settings → Privacy & Security → "Open Anyway" and confirm; this is needed again for each new version; plus the optional Terminal one-liner `xattr -dr com.apple.quarantine <path to Moonglade.app>` with what it does. For `developer-id`: the normal "downloaded from the internet" question. Both say no game files are included.

#### Scenario: Instructions match the mode and the one-liner works
- **GIVEN** `docs/setup.md`, `tools/release/notes/ad-hoc.md` and `tools/release/notes/developer-id.md`
- **WHEN** the check reads them and runs the documented one-liner (path substituted) on a scratch copy of the app that has a quarantine attribute
- **THEN** the ad-hoc text names Privacy & Security, "Open Anyway" and "each new version", and contains the exact one-liner; after the one-liner the scratch copy has no `com.apple.quarantine` attribute and its signature still verifies; the Developer ID text doesn't mention "Open Anyway"; both say no game files are included; and the workflow's notes step uses the file of the active mode
- Check: `signing.open_instructions`

#### Scenario: A friend follows the ad-hoc steps
- **GIVEN** an ad-hoc release downloaded with Safari on another Mac or user account
- **WHEN** a friend follows the release notes
- **THEN** they reach the first-launch window without help, and the wording made sense to them
- [human-verify] The Gatekeeper dialogs and System Settings pane can only be driven by a person on a quarantined download; whether the wording is clear is a judgement.
