#!/bin/bash
# PreCompact: warn when the STATUS.md handoff on origin/main is older than work on a change/* branch.
repo="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
git -C "$repo" fetch -q origin main 2>/dev/null
h=$(git -C "$repo" log -1 --format=%ct origin/main -- STATUS.md 2>/dev/null)
read -r b ref < <(git -C "$repo" for-each-ref --sort=-committerdate --format='%(committerdate:unix) %(refname:short)' 'refs/heads/change/*' | head -1)
if [ -n "$h" ] && [ -n "$b" ] && [ "$b" -gt "$h" ]; then
  msg="Handoff may be stale: $ref has commits $(( (b - h) / 60 )) min newer than the STATUS.md handoff on origin/main. Refresh it after compaction."
  jq -n --arg m "$msg" '{systemMessage:$m}'
fi
exit 0
