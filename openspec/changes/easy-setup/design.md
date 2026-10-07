# Design: Easy setup

## Shape
- `./bootstrap` (repo root, POSIX sh): check Python ≥3.10 is available. If it isn't (a fresh Mac), print the Homebrew line
  to install it and stop. Then exec `harness/hs bootstrap "$@"`.
- `harness/hslib/bootstrap.py`: an ordered list of steps, each with `done()` (a cheap, honest check that the output exists and
  is current, never a self-written "done" marker alone), `run()`, a plain-language label, and a recovery hint on failure. Steps:
  1. preflight: arm64, macOS ≥ the documented minimum, Rosetta (`arch -x86_64 /usr/bin/true`), Xcode CLT
     (`xcode-select -p`), Homebrew at /opt/homebrew, free disk vs the documented need;
  2. Brewfile (`brew bundle check`, then `brew bundle` with consent);
  3. vendor sync + `hs build --clean`;
  4. server data download (pinned, sha256-verified) + unpack;
  5. DB init + first `hs up`/`hs down`;
  6. `hs wine-install`;
  7. `hs client-install <client>` + `client.inspect`;
  8. `hs client-build`;
  9. `hs app`;
  10. the defaults written via the existing settings code.

  Reuse the existing `hs` subcommands; don't duplicate their logic.
- Download cache: `HS_DOWNLOAD_CACHE` (a folder of pinned files, each verified by sha256) lets checks and repeat setups
  avoid re-downloading. A test switch, refused outside the harness unless it is just a cache folder, which is safe
  because every file is verified anyway.
- `setup.fresh_clone` is a **slow-tier** check (it builds a whole second install; ~30–45 min with ccache). The verifier
  runs it once in this change; the weekly slow sweep keeps it honest. The scratch clone uses the same ports, so it runs
  only when the main stack is down; it must restore nothing in the main checkout and must not touch the player's data.
- **F28:** route every harness-shaped `./play`/app run to a scratch play client (`HS_PLAY_CLIENT_DIR`) or refuse it;
  `client.stock_mode` and `play.loop_clean_exit` are the known offenders.
- **F30:** record the repo path the build and install were made for (the install record); compare at `hs up`/`./play`.
- Client-data and VMaNGOS sha256: compute from the pinned URLs once, record in fork.lock with the date.

## Open questions for the builder (answer in "As built")
- The minimum macOS version that the Wine runtime and our launcher actually need (cite the evidence).
- Whether a second macOS account can use the dev Mac's /opt/homebrew read-only (it should); document it for friends.
