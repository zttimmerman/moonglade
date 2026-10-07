# Spec Delta: server-build

## ADDED Requirements

### Requirement: A moved repo is detected
If the server binaries, the build tree or the dev-only baseline build were made in a different repo folder than the current one, `hs up`, `./play` and `hs build` SHALL say so plainly and either rebuild (`hs build`) or refuse with the exact command to run (`hs up`, `./play`). The baseline build SHALL discard a build tree made elsewhere and rebuild by itself.

#### Scenario: Moved repo
- **GIVEN** a scratch build tree and install recorded as made in another folder (a scratch copy with the old path planted in its CMakeCache and install record)
- **WHEN** `hs up` and `hs build` run against it, and the baseline build runs against a planted foreign CMakeCache
- **THEN** `hs up` refuses before starting anything, naming the old and new folder and `hs build --force-clean`; `hs build` rebuilds from empty; the baseline discards the foreign cache and builds
- Check: `build.moved_repo`

### Requirement: Every download is pinned by hash
Every file that setup downloads (server data, Wine runtime, VMaNGOS data, Ghidra) SHALL be pinned in fork.lock by URL and sha256, and verified before use. A mismatch SHALL stop with a clear message and leave no partial file in place.

#### Scenario: Pinned downloads
- **GIVEN** fork.lock
- **WHEN** the pins are read, and a download is fed a file with a wrong hash (scratch cache)
- **THEN** every download URL has a sha256; the bad file is refused, removed, and nothing is unpacked
- Check: `build.downloads_pinned`
