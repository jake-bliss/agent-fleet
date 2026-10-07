# The CI gate: nothing reaches remote CI unverified

Remote CI and PR review bots are shared, rate-limited and often paid for. They should confirm a result you already know, not discover it. So before a branch is pushed, three things are true of the exact commit you are pushing:

1. The repo's own CI jobs ran locally and passed (`ci-preflight`).
2. A second model (Codex) reviewed it (`review-receipt codex pass`).
3. A first model (Claude) reviewed this commit, an ancestor of it, or the same diff before a rebase, with no newer failing verdict (`review-receipt claude pass`).

A `pre-push` hook enforces this. If any part is missing, the push is refused and the hook says which one.

## Why these rules

- CI is checked on the exact SHA because a rebase or a one-line fix changes what the code runs against. A green run is not inheritable.
- The second-model review is also exact-SHA, so every pushed line has had at least one independent review, fix commits included.
- The first-model review carries forward to fix commits. The usual loop is: first model reviews once, a worker fixes, the second model reviews the fixes. Requiring a fresh first-model review for each fix commit re-spends effort on code it already passed. A newer failing verdict on the same lineage still overrides an older pass.
- Two different models review because they miss different things. When they disagree, check the disputed line yourself.

## Mechanics

**Receipt.** `ci-preflight` writes `<git-dir>/adw-receipt.json`: the SHA, branch, repo key, a status, per-job results, and the reviews recorded for that SHA. A new commit starts a clean receipt.

| status | meaning | passes the gate |
|---|---|---|
| `green` | every job CI would run for this diff passed | yes |
| `red` | at least one job failed | no |
| `partial` | `--quick` skipped the slow jobs | no |
| `subset` | `--only` ran one job | no |
| `empty` | every job was skipped, so nothing was verified | no |
| `tooling` | a job could not run because a tool was missing | no |

`partial`, `subset` and `empty` exist so a fast iteration run can never mint a pushable receipt. `tooling` exists so that "my environment is broken" is never confused with "the code is broken": a job whose `requires` tool is absent, or that exits 127, is reported as a tooling error with its own message and exit code (3), and does not count as a code failure. When `node` is not on `PATH` (agent shells do not load an interactive rc), `ci-preflight` looks for it in mise, nvm, fnm, asdf and Homebrew before running. Read the per-job list, not just the last line: a gate whose red means "my tooling is missing", or whose green means "the job was skipped", is trusted and wrong.

**Review history.** `review-receipt` appends every verdict to `<git-dir>/adw-review-history.json` with the SHA and a fingerprint of the branch diff (git's raw diff against the merge base, which captures paths, modes and blob ids). The hook uses the fingerprint to recognise the same diff after a pure rebase.

**Hook.** `install-prepush-gate` sets `core.hooksPath` to `<git-common-dir>/adw-hooks` and chains to whatever hooks the repo already had (a hook manager such as husky keeps working; the original path is saved in `adw.prevHooksPath`, so `--uninstall` restores it). Set `git config adw.chainPrePush false` to skip the repo's own pre-push after the gate passes, if `ci-preflight` already covers it. A package install can reset `core.hooksPath`; `ci-preflight` notices and reinstalls, and `install-prepush-gate --status` shows what is wired where. Deleting a ref, and pushing a commit already on the remote, pass straight through.

**Config.** `ci-preflight` reads `$ADW_HOME/ci-parity.json` (see `config/ci-parity.example.json`). Each repo lists its jobs in the order CI runs them, with these keys: `job`, `name`, `cmd`, `dir`, `when` (path prefixes, mirroring the workflow's paths filter), `slow`, `destructive` (needs `--include-destructive`), `db`, `env`, `requires`. A job whose paths are untouched is skipped exactly as CI skips it. Run `ci-preflight --audit` after a workflow changes: it flags any listed workflow file newer than the config.

**Test databases.** DB-backed jobs would deadlock each other if every worktree shared one test database. With `db_isolation` configured and honoured by the repo's `database.yml`, each worktree gets a private database name and no lock is taken. Without it, DB-backed jobs take a per-repo lock and queue. Two `ci-preflight` runs in the same worktree still share a database: do not start a second one while one is running, and do not run the suite directly beside a preflight.

## The optional remote CI box

If your laptop is slow or busy, point `hosts` in `ci-parity.json` at an SSH-reachable Linux machine and set `default_host` on a repo (or pass `--host <name>`). `ci-preflight` ships HEAD as a git bundle, runs every job there, copies failed-job logs back, and writes the receipt locally. It falls back to running locally if the box is unreachable or the working tree is dirty (the box tests the commit, not your edits).

Host settings: `ssh`, `slots`, `pg_container`, `memory_max`, `memory_swap_max`, and per repo `clone`, `worktree`, `env`, `prepare`, `db_like`.

Sizing advice:

- Such a box is memory-bound, not CPU-bound. A test worker for a large app can take well over a gigabyte, so one worker per core can exhaust memory and lock the machine up. Measure `free -g` during a real run before raising workers or slots; more RAM is the lever for more throughput.
- Set `memory_max`. The whole run (runner, jobs, every test worker) is placed in a user systemd scope with a hard cap, so a run that outgrows the box is killed on its own instead of taking the host down. `memory_swap_max` lets cold memory page out before the cap kills the run; `0` is a hard cap.
- Keep `slots` at 1 unless you have measured headroom. Each slot is its own worktree with its own test databases.
- Runs are first-come-first-served: each waiter takes a ticket named by its arrival time, and only the first `slots` live tickets may try for a slot. Without this, waiters that poll for a free lock let whoever polled first win, and a run can starve.
- Before and after every run the runner kills any process whose working directory is inside the slot and drops the slot's test databases, so a crashed run cannot poison the next.
- Keep production secrets off this box.

## When `--no-verify` is acceptable

Decide, do not ask, and say in one line which case applied.

Acceptable:

- `ci-preflight` reports `empty`: the diff touches nothing CI checks (docs, markdown, agent config).
- The branch is a pure `git revert` of commits already on the default branch.
- `ci-preflight` is green and the reviews the gate needs are recorded, but the hook still blocks. That is a fault in the gate tooling; fix it or note it afterwards.

Never:

- Past a red preflight. Fix the failure.
- Past a `tooling` result. Fix the environment and re-run; the code was not verified.
- With a missing or failing review. Get the review.
