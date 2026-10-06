# Design: Rename to Moonglade
- Bundle: app.py constants (name, id, DESIGNATED), launcher.m strings and notification names, dockshim display name and notification names, polish/app check expectations, Info.plist, `dist/Moonglade.app`. Remove the stale `dist/Hearthstone.app` after the new one is built (gitignored build output).
- TCC: the first LaunchServices open of the new bundle prompts for Documents access. The builder must **pause and ask the orchestrator** before the first open of the new bundle, so Zach can click Allow (the same procedure as launcher-app). Never touch TCC.
- Naming guard: a quick-tier check scans tracked, current files for "Hearthstone" (case-insensitive) outside an allowlist:
  - archived changes, research docs, git history;
  - ADR bodies;
  - Blizzard game data and strings (the in-game item named Hearthstone);
  - the rename change itself, and this guard's own source.
  It fails on any other hit.
- Folder rename script (`tools/rename-folder.sh`, run by Zach with Claude Code closed):
  - `mv ~/Documents/repos/[redacted] ~/Documents/repos/project-moonglade`;
  - move `~/.claude/projects/[redacted]-Documents-repos-[redacted]` to the new encoded path (memory + transcripts);
  - update `git worktree` paths (`git worktree repair`);
  - remind Zach to update anything that hard-codes the old path (Dock pin, etc.).
  It prints what it would do (dry run) unless given `--apply`.
- As built: the script is also resumable: if `--apply` is killed after moving the repo, the next `--apply` finishes the remaining steps (data-folder move, worktree repair). It refuses when the destination exists from an unrelated folder, when the source is not a git checkout, or when a process still has its working directory inside the old folder.
- Cycle 2: harness runs never write the player's own play client: `launch.client_config_step` (realmlist + Wine defaults), resolution and `frontmenu.write` all follow `play.resolution_gate` (harness signs + own client). Real-client harness checks use scratch play clients; `./play` refuses a harness-shaped real-client run on the player's client. Test switch `HS_TEST_OWN_CLIENT` (refused by plain ./play) makes a scratch client count as the player's own, for checks.
