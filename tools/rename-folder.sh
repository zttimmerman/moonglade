#!/bin/bash
# Rename the repo folder to its new project name, and move Claude Code's per-project data with it
# (openspec/changes/rename-moonglade, design.md "Folder rename script"). Run it yourself, with Claude Code closed,
# from OUTSIDE the folder (a shell whose working directory is inside it counts as "still open" and is refused):
#
#   cd ~ && ~/Documents/repos/[redacted]/tools/rename-folder.sh            dry run: print the plan (default)
#   cd ~ && ~/Documents/repos/[redacted]/tools/rename-folder.sh --apply    do it
#   rename-folder.sh [--apply] [--claude-projects DIR] [OLD_REPO NEW_REPO]
#
# Defaults: OLD_REPO and NEW_REPO are the real folders below, DIR is ~/.claude/projects. The harness check
# repo.rename_script only ever runs this against scratch paths in a temp dir.
#
# What --apply does:
#   1. mv OLD_REPO NEW_REPO
#   2. mv DIR/<encoded OLD_REPO> DIR/<encoded NEW_REPO> (Claude Code's memory and transcripts for this folder), and the
#      same for each linked git worktree that lives inside OLD_REPO and has its own folder in DIR. Claude Code names
#      these folders after the absolute path with every character that isn't a letter or digit turned into "-"
#      ([redacted]/x -> [redacted]-x).
#   3. git -C NEW_REPO worktree repair <each linked worktree's new path>, so both sides of every worktree link point
#      at the new paths again.
# It refuses (and changes nothing) when NEW_REPO or a destination folder in DIR already exists, when OLD_REPO isn't
# a git checkout, or when any running process has its working directory inside OLD_REPO (Claude Code still open).
# If an earlier --apply was stopped after step 1 (OLD_REPO gone, NEW_REPO a git checkout), running it again finishes
# steps 2 and 3.
set -euo pipefail

DEFAULT_OLD="$HOME/Documents/repos/[redacted]"
DEFAULT_NEW="$HOME/Documents/repos/project-moonglade"

APPLY=0
PROJECTS="$HOME/.claude/projects"
POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --claude-projects) shift; PROJECTS="${1:?--claude-projects needs a directory}" ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) POS+=("$1") ;;
  esac
  shift
done
case ${#POS[@]} in
  0) OLD="$DEFAULT_OLD"; NEW="$DEFAULT_NEW" ;;
  2) OLD="${POS[0]}"; NEW="${POS[1]}" ;;
  *) echo "give both OLD_REPO and NEW_REPO, or neither" >&2; exit 2 ;;
esac
OLD="${OLD%/}"; NEW="${NEW%/}"; PROJECTS="${PROJECTS%/}"
for p in "$OLD" "$NEW" "$PROJECTS"; do
  case "$p" in /*) ;; *) echo "paths must be absolute: $p" >&2; exit 2 ;; esac
done

encode() { printf '%s' "$1" | LC_ALL=C sed 's/[^A-Za-z0-9]/-/g'; }

fail() { echo "REFUSED: $*" >&2; echo "Nothing was changed." >&2; exit 1; }

# ---- preflight (both modes)
# RESUME: an earlier --apply was stopped after moving the repo (OLD gone, NEW a git checkout); finish the remaining
# steps (data folders, worktree repair) instead of refusing.
RESUME=0
if [ ! -e "$OLD" ] && [ -e "$NEW/.git" ]; then
  RESUME=1
else
  [ -d "$OLD" ] || fail "$OLD doesn't exist"
  [ -e "$OLD/.git" ] || fail "$OLD isn't a git checkout"
  [ ! -e "$NEW" ] || fail "$NEW already exists"
fi
REPO_NOW="$OLD"; [ $RESUME = 1 ] && REPO_NOW="$NEW"
[ "$(dirname "$NEW")" != "" ] && [ -d "$(dirname "$NEW")" ] || fail "the parent of $NEW doesn't exist"
case "$NEW/" in "$OLD"/*) fail "$NEW is inside $OLD" ;; esac

# linked worktrees (not the main one), as recorded by git now
WTS=()
while IFS= read -r line; do
  case "$line" in worktree\ *) WTS+=("${line#worktree }") ;; esac
done < <(git -C "$REPO_NOW" worktree list --porcelain)
LINKED=()
for w in "${WTS[@]:1}"; do LINKED+=("$w"); done

newpath() {   # a path's location after the move (unchanged when it isn't inside OLD)
  case "$1/" in "$OLD"/*) printf '%s' "$NEW${1#"$OLD"}" ;; *) printf '%s' "$1" ;; esac
}

# project-data folders to move: the repo's own, plus one per linked worktree inside the repo
MOVES_FROM=(); MOVES_TO=()
add_move() {
  local from="$PROJECTS/$(encode "$1")" to="$PROJECTS/$(encode "$2")"
  if [ -d "$from" ]; then
    [ ! -e "$to" ] || fail "$to already exists"
    MOVES_FROM+=("$from"); MOVES_TO+=("$to")
  fi
}
add_move "$OLD" "$NEW"
MAIN_DATA_FOUND=${#MOVES_FROM[@]}
for w in ${LINKED[@]+"${LINKED[@]}"}; do
  case "$w/" in "$OLD"/*) add_move "$w" "$(newpath "$w")" ;; esac
done

# Claude Code (or anything else) still working inside the folder. This script's own process tree (itself and the
# subshells, lsof and awk it starts) is left out; whatever started it (a shell, Claude Code) is not.
# One lsof pass gives every process's cwd and parent pid (lsof -R); ps isn't used (setuid, refused under sandboxes).
BUSY=$(lsof -a -d cwd -FpRcn 2>/dev/null | awk -v o="$OLD" -v self="$$" '
  /^p/ {pid=substr($0,2)} /^R/ {par[pid]=substr($0,2)} /^c/ {c[pid]=substr($0,2)}
  /^n/ {n=substr($0,2); if (n==o || index(n, o "/")==1) { hit[pid]=n } }
  function mine(p,   q, k) { q = p; for (k = 0; k < 64 && q != "" && q != "0"; k++) { if (q == self) return 1; q = par[q] } return 0 }
  END { for (p in hit) if (!mine(p)) print p " " c[p] " (" hit[p] ")" }' | sort -n || true)
SELF_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

echo "Rename plan ($([ $APPLY = 1 ] && echo APPLY || echo 'dry run: nothing will change; add --apply to do it')):"
if [ $RESUME = 1 ]; then
  echo "  1. move the repo:      already done ($NEW); resuming an interrupted --apply"
else
  echo "  1. move the repo:      $OLD"
  echo "                      -> $NEW"
fi
if [ ${#MOVES_FROM[@]} -eq 0 ]; then
  echo "  2. Claude Code data:   none found in $PROJECTS for this folder (looked for $(encode "$OLD"))"
else
  for i in "${!MOVES_FROM[@]}"; do
    echo "  2. Claude Code data:   ${MOVES_FROM[$i]}"
    echo "                      -> ${MOVES_TO[$i]}"
  done
fi
[ "$MAIN_DATA_FOUND" -gt 0 ] || echo "     WARNING: no Claude Code folder named $(encode "$OLD"); memory won't follow the rename."
if [ ${#LINKED[@]} -eq 0 ]; then
  echo "  3. git worktrees:      none linked; git worktree repair runs anyway"
else
  for w in "${LINKED[@]}"; do echo "  3. git worktree:       $w -> $(newpath "$w") (git worktree repair)"; done
fi
if [ -n "$BUSY" ]; then
  echo "  Still open inside the folder (pid, program, working directory):"
  echo "$BUSY" | sed 's/^/     /'
fi

echo "  Not moved: Claude Code's per-folder entries in ~/.claude.json (trust, allowed tools) are keyed by the path,"
echo "  so Claude Code asks to trust the new folder once."
if [ $APPLY = 0 ]; then
  [ -z "$BUSY" ] || echo "Close those (Claude Code included) before running with --apply."
  echo "To do it: close Claude Code, then run from outside the folder:"
  echo "  cd ~ && \"$SELF_PATH\" --apply"
  exit 0
fi
[ -z "$BUSY" ] || fail "processes are still working inside $OLD (close Claude Code, and run from outside: cd ~ && \"$SELF_PATH\" --apply)"

# ---- apply
if [ $RESUME = 0 ]; then
  mv "$OLD" "$NEW"
  echo "moved $OLD -> $NEW"
fi
i=0
while [ $i -lt ${#MOVES_FROM[@]} ]; do
  mv "${MOVES_FROM[$i]}" "${MOVES_TO[$i]}"
  echo "moved ${MOVES_FROM[$i]} -> ${MOVES_TO[$i]}"
  i=$((i + 1))
done
REPAIR=()
for w in ${LINKED[@]+"${LINKED[@]}"}; do REPAIR+=("$(newpath "$w")"); done
git -C "$NEW" worktree repair ${REPAIR[@]+"${REPAIR[@]}"}
echo "git worktree list:"
git -C "$NEW" worktree list | sed 's/^/  /'
echo 'Done. Things that still remember the old path, to update by hand:'
echo '  - a Dock or Finder sidebar item pointing at the old folder;'
echo '  - the app: rebuild it (harness/hs app) so it records the new folder;'
echo '  - Claude Code asks to trust the new folder once (its ~/.claude.json per-folder entries, trust and allowed tools,'
echo '    are keyed by path and are not moved);'
echo "  - the git remote, if the GitHub repo was renamed: git -C \"$NEW\" remote set-url origin <new url>."
