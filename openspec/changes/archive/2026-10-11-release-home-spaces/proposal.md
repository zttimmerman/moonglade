## Why
The first real install of v0.1.0, on a friend's Mac, stopped at "Moonglade couldn't create your game world". The release keeps its data in `~/Library/Application Support/Moonglade`, and that path has a space. AzerothCore reads the servers' `-c` option as a filesystem path through boost::program_options, which rejects a path containing whitespace:

    the argument ('…/Application Support/Moonglade/etc/authserver.conf') for option '--config' is invalid

(authserver/Main.cpp:296, worldserver/Main.cpp:739). Every test HOME was a short path without spaces (`…/release-scratch/fl-<pid>/h`), so no check saw it.

## Second bug (v0.1.1, same install)
With v0.1.1 the setup finished, but the world server asserted at the first client login: `ASSERTION FAILED ARC4.cpp:27 result == 1`. The world session cipher is RC4, which OpenSSL 3 keeps in its legacy provider (`ossl-modules/legacy.dylib`). AzerothCore loads it with `OSSL_PROVIDER_load(nullptr, "legacy")` (OpenSSLCrypto.cpp:46), which searches OPENSSL_MODULES or the build's Homebrew modules dir. The release bundled libcrypto but not legacy.dylib, so it worked only on Macs with Homebrew's openssl@3. No release check connected a client to the world under the no-Homebrew sandbox.

## What changes
- `stack.conf_value`: when the config path has whitespace, the servers get it relative to their working directory (release: `HOME/run`, so `../etc/authserver.conf`). Process discovery accepts both forms.
- `tools/release/release.py` bundles `legacy.dylib` into `server/lib` (delocated against the bundled libcrypto); `stack.spawn_env` sets `OPENSSL_MODULES` to `server/lib` in release mode. `release.stack_from_payload` probes the legacy provider and RC4 under the no-Homebrew sandbox, with a built-in counterexample.
- Every scratch release HOME in the release and first-launch checks has a space in its name (`h s`, `h2 s`, `home s`), so the whole release path runs with a spaced HOME from now on.

## Impact
- `harness/hslib/stack.py`, `harness/hslib/checks/release_runtime_checks.py`, `harness/hslib/checks/firstlaunch_checks.py`.
- Ships as v0.1.1 (spaces) and v0.1.2 (legacy provider).
- Upstream: AzerothCore's `-c` parsing is unchanged upstream (same `value<fs::path>`); a report upstream is a candidate for the F17 contributions list.

## Spec-change log
- 2026-10-11: added "A release HOME path with spaces works" (a tightening).
- 2026-10-11: added "The servers' OpenSSL works without Homebrew" (a tightening).
