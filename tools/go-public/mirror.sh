#!/bin/bash
# Make a scratch mirror clone of this repo's origin for tools/go-public/rewrite.sh, with pushing disabled.
#   tools/go-public/mirror.sh <new-empty-dir>
# A real `git clone --mirror` over the network: a separate object store, never a linked or local copy.
set -euo pipefail
dest=${1:?usage: mirror.sh <new-empty-dir>}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
if [ -e "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
  echo "REFUSED: $dest exists and isn't empty" >&2; exit 3
fi
url=$(git -C "$repo" remote get-url origin)
case "$url" in /*|file:*|./*|../*) echo "REFUSED: origin is a local path ($url); the mirror must be a real clone" >&2; exit 3;; esac
git clone -q --mirror "$url" "$dest"
git -C "$dest" remote set-url --push origin no-push
echo "mirror: $dest (from $url; push disabled)"
