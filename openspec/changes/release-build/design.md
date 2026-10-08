## Research summary (Plan agent, 2026-10-08)
- **Server dylib closure:** libmysqlclient.24; boost (filesystem, program_options, iostreams, random, thread, atomic, chrono, date_time, container); openssl@3; readline (worldserver only); transitive xz, zstd and zlib. About 15 MB. Both binaries carry an LC_RPATH to the dev `server/lib`.
- **mysqld:** Homebrew's links ~80 abseil dylibs plus ICU and protobuf. Use Oracle's official arm64 tarball instead: it uses @rpath and ships its own SSL. The CI server build uses its headers and lib, so the client lib matches.
- **Python:** the harness is stdlib-only and needs ≥3.10. `/usr/bin/python3` is a Command Line Tools stub, so it can't be used. Use python-build-standalone 3.13 install_only_stripped (~18 MB compressed).
- **Downloads:** Wine (WoWSilicon r17) and client-data stay first-launch downloads (LGPL source obligations and Blizzard-derived data stay off our release).
- **Compiled-in `_CONF_DIR`:** release servers get module keys as `AC_*` env (proven in lane B, ADR-0011). That belongs to `release-first-launch`; this change only records the conf-dist files.
- **SQL source dir for the DB updater:** core `data/sql/base` + `updates`, playerbots `data/sql`, and our modules' sql; not old/ or archive/. ~45 MB compressed.
- **Estimated download:** ~120 MB.

## Decisions (orchestrator, 2026-10-08)
- **Oracle MySQL, version:** 8.4 LTS unless the dev stack's 9.x schema/updater needs 9.x; the builder checks and records which. Dev parity is a later follow-up.
- **readline:** off (`-DWITH_READLINE` or equivalent); friends never use the server console. This avoids shipping a GPL-3 source.
- **zip now, dmg later:** the dmg comes with `release-signing` if Zach picks Developer ID.
- **DB template:** not in this change. Measure the first-start split (SQL import vs bot setup) in `release-first-launch` first.
- **CI:** the workflow file lands now and is validated locally (actionlint if available via pip, else a YAML schema check). It runs on GitHub only after the repo is public.
- **Local packaging runs on this Mac:** a portable build in `env/release/build` with ccache (power guard applies, `--jobs` default). Oracle MySQL and PBS are downloaded into `env/release/cache`. Never `brew install` locally: CI installs build deps on the runner; locally the existing Homebrew deps are used read-only for the build, and delocate copies them into the payload.
- **"Runs without Homebrew" locally:** `sandbox-exec` with a deny-read profile for `/opt/homebrew` and `/usr/local`. No system changes; the CI smoke additionally moves Homebrew aside on the runner.
