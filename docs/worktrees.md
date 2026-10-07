# Worktrees

## One worktree per task

Every task that changes code gets its own git worktree on its own branch. Concurrent agent sessions then cannot overwrite each other's files or confuse each other's test runs. Read-only work (research, review, debugging) does not need one. Use `git worktree` directly, Worktrunk (`wt`), or `cow-worktree`, which produces an ordinary worktree that all of them see.

```bash
cow-worktree feature/short-task-name                   # branch off the repo's default branch
cow-worktree feature/x --base release/1.2              # branch off something else
cow-worktree feature/x --clean                         # leave gitignored files behind
cd "$(cow-worktree feature/x --print-path)"            # scripted use: prints the path only
```

The default location is `<primary checkout>.<branch with / as ->`, next to the primary. Use `--path` to change it.

## What cow-worktree does

`git worktree add` writes every tracked file. `cow-worktree` registers the worktree with `--no-checkout`, then seeds it with APFS `clonefile(2)` clones from the nearest existing worktree, so git only rewrites files that differ between that donor and your base. Gitignored files (`node_modules`, build caches, vendored gems) are cloned too, so the worktree comes up warm and no install step has to run. Untracked scratch files from the donor are dropped, uncommitted donor edits are reverted, and absolute symlinks that pointed into the donor are re-pointed at the new worktree.

## Four pitfalls

- **`du` lies about copy-on-write trees.** It counts cloned blocks in full, so it reports the donor's size. Only `df` deltas measure a clone honestly.
- **The donor is the whole game.** The saving comes from a donor already near the base commit. The script searches every worktree of the repo and picks the nearest. If all are far from the base, git rewrites nearly everything and the run costs the same as a plain add. Passing `--donor` by hand is rarely right, and the primary checkout is often a bad donor if it sits far behind.
- **Cloning is per volume.** `cp -c` silently degrades to a full copy across volumes, so the script probes the target directory first and falls back to a plain `git worktree add` when cloning is unavailable. It is always safe to run; off APFS it simply stops being cheap.
- **Gitignored files are cloned by default.** That is deliberate, since it removes the cold start. Pass `--clean` when a stale `node_modules`, a warm cache or a leftover `log/` would be wrong.

## Private test database per worktree

Worktrees isolate files, not the database. If every worktree points at one local test database, two suites running at once deadlock each other and report failures that have nothing to do with the code. Give each worktree a private database name (for example through an environment variable that your `database.yml` reads) and configure it as `db_isolation` in `ci-parity.json`, so `ci-preflight` sets it automatically and skips its per-repo lock. The name is per worktree, not per run, so the schema loads once and later runs reuse it. It does not protect two runs in the same worktree.

Treat a shared development Postgres as disposable: it is acceptable to run it with durability settings turned off for speed, as long as nothing in it cannot be regenerated from migrations and seeds.

## Pruning

Worktrees accumulate: one per task adds up to dozens within weeks, each with its test databases. Prune on a schedule (weekly is enough):

```bash
prune-worktrees ~/projects/web-app            # dry run: lists what would go
prune-worktrees ~/projects/web-app --apply
```

It removes a worktree only when its branch has a merged or closed PR (asked of GitHub through `gh`, since squash-merged branches look unmerged to `git branch --merged`) and the tree is clean. It never uses `--force`, so a worktree with uncommitted work refuses and survives, and it never touches the primary checkout, detached worktrees, branches with an open PR, or branches with no PR at all (unpushed work). Drop orphaned per-worktree test databases on the same schedule, matching your naming convention. Run both from launchd or cron.
