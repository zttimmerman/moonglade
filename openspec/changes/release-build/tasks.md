## 1. Inputs and build
- [ ] 1.1 fork.lock pins: Oracle MySQL (binary + source tarball), python-build-standalone; hash-checked fetch into env/release/cache
- [ ] 1.2 Portable server build in env/release/build (rpath, deployment target, readline off, Oracle MySQL client lib), never touching build/ or server/
## 2. Payload
- [ ] 2.1 tools/release/delocate.py (closure copy, @rpath rewrite, ad-hoc re-sign, forbidden-path gate)
- [ ] 2.2 tools/release/package.sh: payload tree (server, mysql, python, harness/play/patches/client-mods/branding, pruned SQL, prebuilt launcher + dockshim, VERSION), THIRD_PARTY, source assets, SHA256SUMS, zip
## 3. CI and checks
- [ ] 3.1 .github/workflows/release.yml (tag push, macOS arm64, ccache, smoke with Homebrew moved aside, gh release create); validated locally, not run
- [ ] 3.2 Checks release.no_abs_paths, release.runs_without_homebrew, release.no_game_files, release.source_alongside, release.pins, release.dev_untouched (each with a counterexample; tier them sensibly: packaging is slow)
- [ ] 3.3 NOTICE/README pointers; design.md as built
