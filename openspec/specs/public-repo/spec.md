# public-repo Specification

## Purpose
TBD - created by archiving change go-public. Update Purpose after archive.

## Requirements

### Requirement: Licence and notices
The repo SHALL carry the GPL-2.0-or-later licence text in `LICENSE`, a `NOTICE` naming the OpenSpec-generated MIT files, the VMaNGOS-derived data and its licence, the branding terms, and a "not affiliated with or endorsed by Blizzard Entertainment" disclaimer. The README SHALL state the licence, the disclaimer and "bring your own client; no game files are distributed".

#### Scenario: Licence files present and consistent
- **GIVEN** the tracked tree
- **WHEN** the licence check runs
- **THEN** LICENSE is the GPL-2.0 text with an "or (at your option) any later version" notice, NOTICE has every listed item, and the README has the licence and disclaimer lines
- Check: `public.license`

### Requirement: Tracked tree is free of personal data
No tracked file SHALL match the built-in patterns (an email address other than noreply/example addresses, `/Users/<name>/` home paths, Claude Code session URLs, "private repo" / "never redistributed" claims) or any line of the local denylist `env/public-denylist.txt` (untracked, 0600, written by the builder from the audit's §2/§7 findings and Zach's details). The built-in patterns SHALL always run. The denylist part SHALL be required only when the untracked marker `env/public-denylist.required` exists: then a missing or empty denylist SHALL be an error (fail closed). Without the marker and without a denylist (a friend's clone), the denylist part SHALL be skipped and the evidence SHALL say so.

#### Scenario: Clean tree passes
- **GIVEN** the tracked tree after the scrub and the local denylist
- **WHEN** the scrub check runs
- **THEN** it reports zero hits and the number of patterns used
- Check: `public.tree_scrub`

#### Scenario: A planted hit fails
- **GIVEN** a scratch file (staged in a temporary index, never committed) containing one denylist entry and one home path
- **WHEN** the scrub check's self-test runs
- **THEN** it reports both hits with file and line; with the `env/public-denylist.required` marker present, a missing or empty denylist is an error; with the marker absent and no denylist, the check passes on the built-in patterns and its evidence says the denylist part was skipped
- Check: `public.tree_scrub`

### Requirement: No third-party game content tracked
No tracked file SHALL be a client or game asset (MPQ, DBC, EXE, DLL, BLP, M2, WDT/ADT, WAV/MP3/OGG, TTF) or a screenshot under `docs/`. Client patches SHALL stay byte recipes keyed by the stock hash.

#### Scenario: Tree has no game files
- **GIVEN** the tracked tree
- **WHEN** the content check runs
- **THEN** no path has a listed extension or image under `docs/research/img/`, and every image left in the tree is under `branding/`
- Check: `public.no_game_files`

### Requirement: Clean-history dry run
`tools/go-public/rewrite.sh` SHALL rewrite a scratch **mirror clone** (never the working checkout, never pushing) as follows:
- author and committer emails mapped to Zach's GitHub noreply address;
- `Claude-Session:` trailers removed;
- the old project name (the naming guard's `OLD_NAME`) → "Moonglade" in commit messages;
- `__pycache__/` and `docs/research/img/` dropped from all history;
- denylist lines replaced in all blobs.

A scan SHALL then show zero hits in every commit's message, author, committer and blobs. The final tree SHALL equal the scrubbed `main` tree. The tool SHALL refuse to run if its target has a push URL pointing at GitHub.

#### Scenario: Dry run yields clean history
- **GIVEN** a fresh `git clone --mirror` of origin into the scratch area
- **WHEN** the rewrite and the history scan run there
- **THEN** the scan finds zero hits across all refs, `main`'s tree hash equals the scrubbed tree, and nothing was pushed (origin's refs unchanged)
- Check: `public.history_dryrun`

#### Scenario: Refuses a real remote
- **GIVEN** the working checkout (origin = GitHub)
- **WHEN** `tools/go-public/rewrite.sh` is pointed at it
- **THEN** it refuses before changing anything
- Check: `public.history_dryrun`

### Requirement: Go-public runbook
`docs/going-public.md` SHALL list Zach's go steps in order, each with its exact command and how to verify it:
- rewrite;
- force-push all branches and tags;
- edit PR bodies;
- the GitHub Support request text for purging `refs/pull/*`;
- verify the old refs are gone;
- flip visibility;
- re-clone every checkout and worktree;
- set the noreply email and "Block command line pushes that expose my email".

#### Scenario: Zach follows the runbook
- **GIVEN** the runbook and the dry-run result
- **WHEN** Zach approves each step at the time
- **THEN** the public repo shows only rewritten history
- [human-verify] Each step is an irreversible, outward action that needs Zach's explicit go (standing rule); the dry run covers the mechanics.
