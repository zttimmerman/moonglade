#!/bin/bash
# Rewrite a scratch mirror clone's history for publishing (openspec/changes/go-public). See rewrite.py for what it
# changes and docs/going-public.md for the steps around it. Never pushes.
#   tools/go-public/rewrite.sh [--denylist FILE] <bare-mirror-dir>
# git-filter-repo comes from PyPI, pinned by hash (requirements.txt), in a repo-local venv: env/tools/venv.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
venv="$repo/env/tools/venv"
if ! "$venv/bin/python3" -c 'import git_filter_repo' 2>/dev/null; then
  echo "installing git-filter-repo into env/tools/venv (pinned, hash-checked)" >&2
  python3 -m venv "$venv"
  "$venv/bin/pip" install -q --require-hashes -r "$here/requirements.txt"
fi
exec "$venv/bin/python3" -I "$here/rewrite.py" "$@"
