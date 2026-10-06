# Spec Delta: harness-reporting

## ADDED Requirements

### Requirement: Old project name doesn't come back
Tracked current files SHALL NOT use the old project name "Hearthstone", except in recorded history and in Blizzard's own game content.

#### Scenario: Naming guard
- **GIVEN** the repository at HEAD
- **WHEN** the naming guard scans tracked files case-insensitively for "hearthstone"
- **THEN** every hit is inside the documented allowlist (archived changes, research docs, ADR bodies, Blizzard game data/strings, this change and the guard itself), and adding the word to any other tracked file makes the guard fail
- Check: `repo.naming`

### Requirement: Folder rename is safe
The folder-rename script SHALL default to a dry run and, with `--apply`, move the repo folder and Claude Code's per-project data together and repair git worktree paths.

#### Scenario: Dry run and apply in a scratch copy
- **GIVEN** a scratch copy of a repo with a worktree, and a scratch Claude project-data folder named after its path
- **WHEN** the script runs without and then with `--apply` against the scratch paths
- **THEN** the dry run changes nothing and prints the plan; the apply moves the folder and the project-data folder to the new encoded name, `git worktree list` shows valid paths, and nothing outside the scratch paths is touched
- Check: `repo.rename_script`
