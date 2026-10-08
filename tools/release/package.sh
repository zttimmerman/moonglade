#!/usr/bin/env bash
# Build a Moonglade prebuilt release (openspec/changes/release-build): the portable server build, the payload tree,
# the zip, the source assets and SHA256SUMS, all under env/release/. See tools/release/release.py for the layout.
#
#   tools/release/package.sh [all | clean fetch build payload assets ...] [--jobs N] [--ignore-power]
#                            [--version V] [--work DIR] [--cache DIR] [--out DIR] [--sync-vendor] [--record FILE]
#
# Locally it uses the Homebrew packages already installed (read-only) and never installs anything; CI installs the
# build dependencies on the runner first (.github/workflows/release.yml).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ $# -eq 0 ]; then set -- all; fi
PY="${MOONGLADE_RELEASE_PYTHON:-python3}"
exec "$PY" -I "$HERE/release.py" "$@"
