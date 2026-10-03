#!/bin/bash
# SessionStart (compact|clear|resume): feed the orchestrator its handoff so a fresh or compacted context resumes from STATUS.md.
repo="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}"
status=$(git -C "$repo" show origin/main:STATUS.md 2>/dev/null || cat "$repo/STATUS.md" 2>/dev/null)
handoff=$(printf '%s\n' "$status" | awk '/^## ▶ Handoff/{on=1} on&&/^## /&&!/Handoff/{exit} on')
[ -n "$handoff" ] || handoff="(no '▶ Handoff' section found in STATUS.md)"
branches=$(git -C "$repo" for-each-ref --sort=-committerdate --format='%(refname:short) %(committerdate:relative) %(subject)' 'refs/heads/change/*' 2>/dev/null | head -5)
mem="$HOME/.claude/projects/$(printf '%s' "${repo%/.claude/worktrees/*}" | tr '/' '-')/memory/MEMORY.md"
ctx="Hearthstone resume context (injected by .claude/hooks/session-context.sh).

STATUS.md handoff (origin/main):
$handoff

Newest change branches:
${branches:-(none)}

Memory index: $mem

Context-survival check: before resuming work, state from memory (1) current milestone, (2) what is in flight and who owns the stack, (3) next step, (4) Zach's standing constraints; compare with the handoff above and git, and tell Zach about any gaps."
jq -n --arg c "$ctx" '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$c}}'
