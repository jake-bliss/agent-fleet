---
name: adw
description: "Drive one segment of build work (one brief → one PR) to merge: a worker builds in an isolated worktree, a fresh reviewer plus Codex review round 1 while local CI runs, Codex alone reviews the fixes, then push and work the PR review bot (max 3 rounds) or surface to the user. Triggers on \"/adw\", \"run adw\", \"adw <repo> <brief>\", \"drive the pipeline\". NOT for one-offs, exploration or debugging; epics use /adw-lead."
---

# ADW — one segment to a merged PR

`/adw <repo-key> <brief-path>` (repo-key ∈ `$ADW_HOME/manifest.json` repos, e.g. `web-app`; it gives
`path`, `base_branch`, `verify`). `$ADW_HOME` defaults to `~/.claude/adw`. Repos are
never moved. Given an epic → STOP, use `/adw-lead`.
The why behind these rules: `references/rationale.md` — read it when a rule seems not to fit.

**Actors.** You (the strongest model): plan-lint, briefs, adjudication, push, bot loop — never read
whole diffs or write code; reviews return conclusions. **Builder**: the `worker` agent (cheaper model,
default) or Codex; works in one assigned worktree, commits there, never pushes, never `--no-verify`.
**6a**: a fresh `reviewer` agent on the strongest model, round 1 only. **Codex**: round 1 beside 6a,
alone on every later review. **`ci-preflight`**: repos with `gate_tier: "fast"` in `ci-parity.json` run
the fast jobs locally and the full suite on GitHub against a draft PR (`--full` is refused there);
others run the full suite locally (or on a remote CI box). **The PR review bot**, if the repo has one, approves.
**The user**: product questions, and PRs the bot won't approve (every PR, in a repo with no bot).

## Hard rules
- **F2** Never build in a repo's main checkout — always a fresh worktree.
- **F4** Private test DB per worktree (`ci-preflight` sets it from `db_isolation` in `ci-parity.json`;
  workers set the same variable), never a shared test database. Workers: targeted tests only,
  single-process, `timeout 600`, never the full suite.
- **F5** Lint the brief against the repo's conventions BEFORE building.
- **F6** Branch from the REMOTE base: `cow-worktree <branch> --base origin/<base>`. Re-fetch + rebase
  before trusting a green PR.
- **F7** Round 1 always includes reviewers that never saw the brief.
- **F8** Segment ≤ ~800 changed lines (adds + dels, excluding generated: schema dumps, lockfiles,
  generated clients, recorded fixtures); indivisible work (one migration + its model) may exceed with a
  stated reason. Brief needs more → stop at plan-lint, return a split to the lead (the user if
  standalone). Build overruns → the builder stops at a coherent boundary and reports; the segment is
  **incomplete**, never pushed as-is, until a revised brief (the lead's Greplan) covers the rest.
- **Codex builder**: same cap, no deep stacks, **6a never skipped**.
  `codex exec -C "<worktree>" -s workspace-write --skip-git-repo-check - < prompt`. Codex can't write
  `.git` — you commit; its fixes also go to Codex. Unattended/overnight runs follow
  `$ADW_HOME/docs/codex-overnight.md`.

## Nodes
0. **Plan-lint** — brief + `<worktree>/CLAUDE.md` (or `.claude/CLAUDE.md`) + the repo's conventions;
   fix house-style traps [F5]. Tier **high** if it touches a query on a tenant-scoped table,
   payments/orders/pricing, a migration, a sync/webhook/job path, auth/permissions, secrets, or a
   surface a lens in `$ADW_HOME/review/lenses/` covers; else **low**; unsure → high. Size vs F8; over →
   split first. Under `/adw-lead`: record `builder` (`claude`|`codex`) in `graph.json`.
1. **Worktree** — `cd <repo path> && cow-worktree claude/<suffix> --base origin/<base>`
   (`$ADW_HOME/docs/worktrees.md`).
2. **Build** — one builder, one worktree, brief written to a scratchpad file. It must require before
   done: targeted tests + **the repo's guard tests** + a caller search for every changed method
   (`$ADW_HOME/review/lenses/callsite-coverage.md`); linter clean on touched files; one commit, no push,
   no `--no-verify`; a report (files, tests + results, callers checked, unresolved); stop at F8's cap;
   **context hygiene**: `rg -n` then narrow line ranges, never whole large files; test/lint output to a
   log file, read back only exit code, failures, summary (never `| tail` alone — it hides the exit
   status); chain related shell steps in one call; each run under `timeout 600`; never wait on, sleep
   for or poll CI. Keep the worker's ID; fixes go back via `SendMessage`.
3. **Round 1** — when the build commit lands, ONE message, all backgrounded: `ci-preflight`; **6a**
   reviewer on the diff vs `origin/<base>` + `$ADW_HOME/review/review-contract.md` + every touched lens,
   **not** the brief (`$ADW_HOME/review/lenses/adversarial-review.md`); **6c**
   `codex-review diff <worktree> --base origin/<base>` (contract + matching lenses built in; prints the verdict only).
   Tier = max(step 0 plan tier, `review-tier <worktree>` on the real diff; high: more than 400 changed lines or a
   risky path; it only goes up). **High** → 6a + 6c. **Low** → 6c only (6a never skipped if Codex built), then
   `review-receipt claude skip "low tier"`; the gate recomputes the tier and refuses a skip on high.
4. **Adjudicate once** — verify every finding in code; where 6a/6c disagree, read the line, say who
   was right. **6b** checklist: DoD, scope fences, the repo's location/shape conventions. Green tests
   and a clean linter are preconditions, not evidence. **Only a contract `fail` blocks** (any CRITICAL,
   or IMPORTANT at high confidence); the rest → **Known / deferred** in the PR body. Record on the
   reviewed commit (carries to fixes): `review-receipt claude pass "<found; fixes required: …>" --found N
   --unique N --sev S` and the same for `codex` (adjudicated-real findings, how many the other model
   missed, worst severity high|med|low|none; it feeds the review ledger, `review-receipt --ledger`) — pass
   = adjudicated, blockers become the fix list. `fail` only when it must be redesigned (a later round-1
   pass clears it). Low tier: record only the codex line plus the claude skip.
5. **Fix → Codex-only delta** — ONE combined fix list. Codex reviews `git diff <round-1 SHA>..HEAD`
   **and** existing behaviour the fixes could break; no second reviewer agent. Run it with
   `-o <scratchpad>/codex-delta.md` and read only the verdict + findings lines. Rerun `ci-preflight`.
   **Fix-streak stop:** round-2 findings *created by* round-1 fixes → redesign (new round 1, both
   reviewers) or surface to the user; never a third patch round. Blocker standing after round 2 → the user.
6. **Gate** — final commit: `ci-preflight` green (`fast-green` in the fast tier), then
   `review-receipt codex pass "<last Codex pass>"`. The hook needs CI + Codex on this SHA, Claude on
   this SHA or an ancestor (or the same diff rebased). `$ADW_HOME/docs/ci-gate.md`.
7. **Push** — fast tier: `git push` + `gh pr create --fill --draft`; ONE backgrounded (under a lead: foreground, see
   **Waits under a lead**) `pr-state <pr> --wait ci > <scratchpad>/checks.log 2>&1` (never poll); read only failing jobs/tests
   (`pr-state <pr> --failures`, or `gh run view --log-failed | rg -m 40 …`). **Red** → worker fix, Codex delta,
   `ci-preflight`, push, still draft; plainly infra (runner shutdown, expired log) or a known flake on
   untouched code → `gh run rerun <id> --failed` once, logged. **Green on current head** →
   `gh pr ready <pr>`. Never ready on red or pending. Full tier: `gh pr create --fill`; a remote-only
   red is a parity gap — fix the code AND `ci-parity.json`.
8. **Review bot loop** — its (and any other bot's) inline comments are findings, same
   exit rule; reply on every thread with the fix commit or why not. Approved for current head → 9.
   **High-risk** (risk capped, "human review required", "needs a human") → the user now. Comments →
   worker fix, Codex delta, `ci-preflight`, push; fast tier non-trivial fix → `gh pr ready --undo`
   first, ready again (`pr-ship <pr>`) only on green GitHub CI. Read findings with
   `pr-state <pr> --findings`; reply with `pr-reply <pr> <comment-url|comment-id|thread-id> "<body>" [--resolve]`. **≤ 3 rounds**, then the user with what's open;
   fix-streak stop applies. No bot: surface to the user for approval — unless the repo is `"merge": "green"` in
   `$ADW_HOME/pr-tools.json` (merge once green).
9. **Merge** — bot approved current head + CI green + `mergeStateStatus` `CLEAN` → `pr-ship <pr>`
   (squash-merges only when those hold; exit 3 = wait, 4 = the user's call) without asking, then ONE
   backgrounded (under a lead: foreground) `pr-state <pr> --wait deployed` (exit 2 = a deploy failed → the user; exit 4 = no deploy ran or it was cancelled/superseded → check the default branch's latest deploy covers the commit, not an escalation). Anything else → the
   user approves or forces: file the ask with the merge option starting `ship <repo>#<n>@<head sha>`; on
   that answer run `pr-ship <pr> --approved-by <ask-id>` (any push voids it).
   The bot's one cycle ended without approval and every open finding is answered (a nit, fixed in a PR commit after
   the finding with a reply starting `Fixed in <sha>`, a verified false positive with a reply starting `False positive:` + `file:line`,
   or a risk the user accepted in an ask naming `<repo>#<n>` whose chosen option starts `accept`) → `pr-ship <pr> --residual "<reason>"`
   (needs verified Opus and Codex receipts on the head; refuses high-risk). A PR whose every new path sits behind a flag that is off, with the flag key in
   the PR body → `pr-ship <pr> --flagged <flag-key>` (also needs an Opus pass that checked flag coverage:
   `review-tier --flagged <flag-key>`, the reviewer's flag-check mode, then `review-receipt claude pass ... --mode flag-check`). Monitor after either merge;
   a regression → revert or turn the flag off at once, then tell the user.

## Stop early — the ONLY interrupts
Business/product question (lettered options) or brief ambiguity code + house style can't settle;
fix-streak stop or blocker after round 2; bot high-risk or 3 rounds unapproved; anything outside merge
authority. Never stop to confirm push, PR open or ready.
"Surface to the user" = an `adw-ask` decision (`$ADW_HOME/docs/CLAUDE.snippet.md`): finding, what was
tried, why stuck, each option's consequence, recommendation — then stop. As `/adw-lead`'s subagent:
return the question to the lead (it files) and don't run `adw-status`. Every `adw-ask new` passes `--class`
(refused without one): gated (wait for the owner) merge, prod-write, money, auth, customer-data, scope, irreversible;
non-gated reversible, design, split, redesign, fix-streak, size, sequencing. With `--recommend` the non-gated ones are
answered at once (read the `[adw-ask <id> answered]` prompt, carry on). Merge asks keep the `ship <repo>#<n>@<sha>`
label and can't use `--default-after`. Relays to the owner or a lead go by `adw-mux send`, never `adw-ask new`.
Standalone terminal tab:
`adw-status waiting "<CI / review bot on PR>" --for <expected>` before a waiting turn ends;
`adw-status done|failed "<one line>"` at the end.

**Waits under a lead.** As `/adw-lead`'s subagent, never end a turn while a PR wait is pending: every turn you
end with background work running wakes the lead, which re-reads its whole context to do nothing. Run the
`pr-state --wait` calls of steps 7-9 in the foreground with `--timeout 9m` and the Bash timeout at 600000;
exit 1 (timeout) means the same call again, nothing in between, for at most 2 h in all (the backgrounded
default); still exit 1 after that, handle it as that step's timeout (`deployed`: as exit 4). Standalone tab:
background them as written.

One `/adw` = one segment = one PR; epics → `/adw-lead`. Avoid stacking on unmerged bases where seams
allow; a stacked branch waits for its base PR to merge, then rebases.
