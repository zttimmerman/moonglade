# Going public: runbook

How `zttimmerman/project-moonglade` goes from private to public with a clean history (openspec change `go-public`; Zach's decisions of 2026-10-06: keep this repo, rewrite its history, purge the PR refs, then flip). **Every step below is outward and irreversible. Each one runs only on Zach's explicit go at the time.** Agents prepare and verify; they never push rewritten history, edit PRs, contact GitHub Support or change repo settings.

Run everything from the main checkout (the folder holding `env/`, `client/` and `data/`), in order. Stop at the first step whose verification fails.

## 0. Preconditions

- `go-public` is merged to `main`, with a verifier PASS and a green full harness run.
- The local denylist exists: `env/public-denylist.txt` (untracked, mode 0600, one literal string per line). `ls -l env/public-denylist.txt` shows `-rw-------`.
- The tree is clean and the dry run passes on `main`:

  ```sh
  git checkout main && git pull --ff-only && git status --short          # prints nothing
  harness/hs run --tier slow --only 'public.*'                         # public.license, tree_scrub, no_game_files, history_dryrun: pass
  ```
- No open PRs (`gh pr list --state open` prints nothing). A rewrite orphans every open PR.
- Nothing else is running against the repo (no builders, verifiers or worktree agents).

## 1. Rewrite (local, reversible)

```sh
tools/go-public/mirror.sh env/tmp/publish.git            # fresh `git clone --mirror` of origin, push disabled
git -C env/tmp/publish.git rev-list --all > env/tmp/old-shas.txt   # for step 5
tools/go-public/rewrite.sh env/tmp/publish.git           # installs git-filter-repo into env/tools/venv on first use
```

What it changes (all refs): author and committer emails → the GitHub noreply address; `Claude-Session:` trailers removed; the old project name → Moonglade in commit messages; `__pycache__/`, `*.pyc` and `docs/research/img/` dropped; every denylist string → `[redacted]` in every file version and message. It refuses a working checkout and any mirror whose push URL points at GitHub.

**Verify:**

```sh
PYTHONPATH=harness python3 -m hslib.publicscan history env/tmp/publish.git   # ends with "0 hit(s)"
git -C env/tmp/publish.git rev-parse 'main^{tree}'; git rev-parse 'main^{tree}'   # the same hash
git -C env/tmp/publish.git log --format='%ae %ce' --all | sort -u                 # only noreply addresses
git -C env/tmp/publish.git remote                                                # prints nothing (origin was removed)
```

If anything is off, delete `env/tmp/publish.git` and start again; nothing has left the Mac yet.

## 2. Force-push all branches and tags (Zach's go)

```sh
git -C env/tmp/publish.git remote add origin https://github.com/zttimmerman/project-moonglade.git
git -C env/tmp/publish.git push --force origin 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'
```

Don't use `--mirror`: GitHub rejects writes to `refs/pull/*` (step 4 handles those). If branch protection rejects the push, Zach lifts it for this push in the repo settings and restores it afterwards.

**Verify:** the remote branch and tag SHAs equal the rewritten ones.

```sh
diff <(git ls-remote origin 'refs/heads/*' 'refs/tags/*' | sort -k2) \
     <(git -C env/tmp/publish.git for-each-ref --format='%(objectname)%09%(refname)' refs/heads refs/tags | sort -k2)
```

`diff` prints nothing. Then remove the remote again: `git -C env/tmp/publish.git remote remove origin`.

## 3. Edit PR bodies and titles (Zach's go)

Save every PR body, scan them, and fix the ones with hits (Claude Code session links, the old project name in titles, denylist strings):

```sh
mkdir -p env/tmp/pr-bodies
for n in $(gh pr list --state all --limit 200 --json number --jq '.[].number'); do
  gh pr view "$n" --json title,body --jq '"# " + .title + "\n\n" + .body' > "env/tmp/pr-bodies/$n.md"
done
PYTHONPATH=harness python3 -m hslib.publicscan files env/tmp/pr-bodies/*.md     # lists file + line per hit
grep -il "$(PYTHONPATH=harness python3 -c 'from hslib.checks.naming_checks import OLD_NAME; print(OLD_NAME)')" env/tmp/pr-bodies/*.md
```

For each PR with a hit: edit its saved body (drop the session-link lines, rename the old project name, remove denylisted text), then

```sh
gh pr edit <n> --body-file env/tmp/pr-bodies/<n>-clean.md     # body without the "# title" line
gh pr edit <n> --title "<new title>"                          # only where the title had a hit
```

Also check PR conversation and review comments: `gh api repos/zttimmerman/project-moonglade/issues/<n>/comments` and `.../pulls/<n>/comments`; edit or delete any comment with a hit in the GitHub UI.

**Verify:** re-run the save loop into a fresh folder and the `publicscan files` scan; it ends with "0 hit(s)".

## 4. Ask GitHub Support to purge the old PR refs (Zach's go)

The owner can't delete `refs/pull/<n>/head`, and those refs keep every old commit reachable (and visible on the PR pages). Use GitHub Support's "Remove sensitive data" request (support.github.com → "Removing sensitive data from a repository"). Text to send:

> Repository: zttimmerman/project-moonglade (currently private).
> I rewrote this repository's history with git-filter-repo to remove personal data (my personal email address in commit metadata, private links in commit messages and some personal notes) and force-pushed every branch and tag. Please:
> 1. remove the cached views and dereference the old commits of all pull requests (#1–#N), so that `refs/pull/*/head` and `refs/pull/*/merge` no longer point at pre-rewrite commits, and
> 2. run garbage collection so that the old commits are no longer reachable by SHA.
> The old commit SHAs are listed in the attached file (old-shas.txt). I'll keep the repository private until you confirm. Thank you.

Attach `env/tmp/old-shas.txt` (commit SHAs only; it holds no personal data). Replace N with the highest PR number (`gh pr list --state all --limit 1 --json number`).

## 5. Verify the old refs and commits are gone

```sh
git ls-remote origin 'refs/pull/*' | while read sha ref; do
  git -C env/tmp/publish.git cat-file -e "$sha^{commit}" 2>/dev/null || echo "OLD: $ref $sha"
done                                                                        # prints nothing
for sha in $(shuf -n 20 env/tmp/old-shas.txt); do
  gh api "repos/zttimmerman/project-moonglade/commits/$sha" --silent 2>/dev/null && echo "STILL THERE: $sha"
done                                                                        # prints nothing (each lookup 404s)
```

Also open two old PR pages in a browser and check that their commit lists show the rewritten commits (or none). Don't flip until all three checks are clean; if Support hasn't finished, wait.

## 6. Flip visibility (Zach's go)

```sh
gh repo edit zttimmerman/project-moonglade --visibility public --accept-visibility-change-consequences
```

**Verify:** `gh repo view zttimmerman/project-moonglade --json visibility` shows `PUBLIC`, and the repo page opens in a private browser window while logged out.

## 7. Re-clone every checkout and worktree

Every existing clone still holds the old history and would push it back. On this Mac:

```sh
git worktree list                                        # remove every extra worktree:
git worktree remove <path>                               # (each one; recreate later from the new history)
git fetch origin --prune
git checkout main && git reset --hard origin/main        # only with a clean `git status`
git for-each-ref --format='%(refname:short)' refs/heads | grep -vx main | xargs -n1 git branch -D
git reflog expire --expire=now --all && git gc --prune=now
```

That is a re-clone for git purposes while keeping the untracked runtime folders (`env/`, `data/`, `client/`, `server/`, `build/`, `vendor/`) where they are; moving them to a fresh clone would break the CMake caches, which hold the absolute path (F30). Any other machine or clone: delete it and clone again.

**Verify:** `git rev-list --all | grep -c -f env/tmp/old-shas.txt` prints `0`, and `harness/hs run --tier quick` passes.

## 8. Keep new commits clean

- Commit as the noreply address: `git config --global user.email 93889833+zttimmerman@users.noreply.github.com` (or per repo).
- GitHub → Settings → Emails: tick **Keep my email addresses private** and **Block command line pushes that expose my email**.
- Decide whether commits keep the `Claude-Session:` trailer (it links to a private session; dropping it is cleaner).
- Self-hosted Actions runners stay off this repo: on a public repo anyone's pull request could run code on the runner.
- `public.tree_scrub` and `public.no_game_files` run in every quick tier; keep `env/public-denylist.txt` up to date when new personal details come up.

Afterwards: delete `env/tmp/publish.git`, `env/tmp/pr-bodies` and `env/tmp/old-shas.txt`.
