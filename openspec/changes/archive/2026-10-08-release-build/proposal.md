## Why
Prebuilt releases (STATUS "Next", Zach 2026-10-07): friends download one file and need no Homebrew or Xcode tools. Design research B-rel (2026-10-08, summarised in design.md) split the work into three changes:
1. `release-build`: this one. A relocatable server + runtime payload, a packaging script and the CI workflow.
2. `release-first-launch`: the APP/HOME split, release mode in the harness, and the first-launch UI.
3. `release-signing`: the signing mode, the navy icon tile everywhere, and dropping the Info.plist "not for redistribution" line.

## What Changes
- Portable server build: `-DCMAKE_INSTALL_RPATH=@executable_path/../lib`, `MACOSX_DEPLOYMENT_TARGET=26.0`, readline off. It goes into its own build dir and never touches the dev `build/` or `server/`.
- MySQL for releases: Oracle's official macOS arm64 tarball, pinned (URL + sha256) in `fork.lock`. Only `mysqld`, `mysql`, `mysqldump`, `lib/` and the needed `share/` are kept.
- Python for releases: python-build-standalone 3.13 `install_only_stripped`, pinned and pruned.
- `tools/release/delocate.py`: copies every non-system dylib into `lib/`, rewrites install names to `@rpath`, re-signs (ad-hoc for now), and fails on any `/opt/homebrew`, `/usr/local` or `/Users/` load command or rpath.
- `tools/release/package.sh`: builds the payload tree, the pruned SQL source directory, `THIRD_PARTY/` licences, `VERSION`, the source assets and `SHA256SUMS`, then the zip.
- `.github/workflows/release.yml`: runs on tag push only, on GitHub-hosted macOS arm64 runners. It is not triggered until the repo is public (free macOS minutes; ADR-0012: no self-hosted runners).
- Wine and the server data (client-data) stay first-launch downloads, never inside a release. Client files are never in a release.

## Capabilities
### New Capabilities
- `release-build`: relocatable release payload and its packaging.

## Impact
New tools/release/, fork.lock pins, harness checks, a CI workflow, NOTICE/THIRD_PARTY. The dev build path and the player's install are unchanged.

## Spec-change log
