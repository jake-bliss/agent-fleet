---
name: adw
description: Drive one segment of build work to a merged PR — the orchestrator model plans, a cheaper worker model builds in an isolated worktree, one review round by a fresh reviewer agent plus Codex runs while full CI runs locally (or on the CI box), Codex alone reviews the fixes, then push and work the PR review bot to approval (max 3 rounds) or surface to the user. Use for a scoped, reviewable segment (one brief → one PR) in a repo listed in the ADW manifest. Triggers on "run adw", "/adw", "drive the pipeline", "run the workflow on this brief", "adw <repo> <brief>". NOT for quick one-offs, exploration, or debugging — just work normally for those.
---

# ADW — AI Developer Workflow driver

Engine and state live in `$ADW_HOME` (default `~/.claude/adw`): `manifest.json`, lenses, briefs, epics.
Repos are never moved — the manifest resolves paths; each run isolates in its own worktree.

**Invocation:** `/adw <repo-key> <brief-path>`   (repo-key ∈ `manifest.repos`, e.g. `web-app`)
If given an epic instead of a segment brief, STOP and decompose first (`/adw-lead`).

## The actors
- **The orchestrator (you, the strongest model)** — plan-lint, briefs, adjudication, git push, the bot
  loop. You do not read whole diffs or write the code; reviews and explores come back as conclusions.
  Every turn re-reads your context, so a file dump here costs its size × every remaining turn.
- **Worker** (`Agent`, `subagent_type: worker`, a cheaper model) — builds and fixes inside one
  assigned worktree. Commits there; never pushes, never `--no-verify`.
- **Fresh reviewer** (`Agent`, `subagent_type: reviewer`, strongest model) — round 1 only.
- **Codex** (`codex exec`) — round 1 alongside the reviewer, and alone on every later review (fix
  deltas, bot-driven fixes). Costs no Claude usage.
- **Local CI** — the repo's full PR CI via `ci-preflight`, on this machine or the CI box (optional).
  No model usage; the most valuable single gate. See `$ADW_HOME/docs/ci-gate.md`.
- **The PR review bot** — a bot whose approval counts toward branch protection, if the repo has one.
- **The user** — product questions, and PRs the bot will not approve (or every PR, in a repo without one).

## Hardening — learned live
- **F2** Never build in a repo's main checkout (it may be detached or dirty). Always a fresh worktree
  off the remote default branch.
- **F4** DB-backed tests use a private database per worktree (`ci-preflight` sets it from `db_isolation` in
  `$ADW_HOME/ci-parity.json`; workers set the same variable), never a shared test database. Workers run targeted
  tests only, single-process, under `timeout 600`. The full suite runs in `ci-preflight` only — a
  worker's local full-suite run can hang for hours.
- **F5** Briefs carry latent house-style bugs. Lint the brief against the repo's conventions BEFORE
  building.
- **F6** Branch from the REMOTE base: `cow-worktree <branch> --base origin/<base>`. Local base refs go
  stale silently. Re-fetch and rebase before trusting a green PR.
- **F7** The orchestrator cannot be the only reviewer of work built to its own brief — same mental
  model twice. Round 1 always includes reviewers that never saw the brief.
- **Codex as builder** is a fallback (huge mechanical refactors, or when workers stall), not the
  default. Then: `codex exec -C "<worktree>" -s workspace-write --skip-git-repo-check - < prompt`,
  and you do the git.

## Node order

Read `$ADW_HOME/manifest.json` for the repo's `path`, `base_branch`, `human_approve` and `verify` commands
(shape: `config/manifest.example.json`). Then:

0. **Plan-lint** — read the brief, `<worktree>/CLAUDE.md` (or `.claude/CLAUDE.md`) and the repo's
   conventions; fix house-style traps in the brief before building to it [F5]. Set the **risk tier**:
   **high** if the brief touches a query on a tenant-scoped table, payments/orders/pricing, a
   migration, a sync/webhook/job path, auth/permissions, secrets, or a surface a lens in
   `$ADW_HOME/review/lenses/` covers. **low** otherwise. Unsure → high.
1. **Worktree** — `cd <repo path> && cow-worktree claude/<suffix> --base origin/<base>`. [F2, F6]
   See `$ADW_HOME/docs/worktrees.md`.
2. **Build (worker)** — one worker, one worktree, a written brief (scratchpad file). The brief must
   require, before it reports done:
   - targeted tests for what changed, **plus the repo's guard tests**, plus a search for the callers
     of every changed method (`$ADW_HOME/review/lenses/callsite-coverage.md`) — targeted runs alone miss these;
   - the repo's linter clean on touched files; one commit in the worktree; no push, no `--no-verify`;
   - a short report: files changed, tests run with results, callers checked, anything unresolved.
   Keep the worker's agent ID — fixes go back to the same worker (`SendMessage`) so it keeps context.
3. **Round 1 — review and full CI in parallel.** The moment the build commit lands, launch in ONE
   message, all backgrounded:
   - `ci-preflight` in the worktree;
   - **6a** a fresh `reviewer` agent: the diff vs `origin/<base>`, `$ADW_HOME/review/review-contract.md`, every
     lens the diff touches — and **not** the brief (`$ADW_HOME/review/lenses/adversarial-review.md`);
   - **6c** Codex: `codex exec -C "<worktree>" -s read-only "<contract + lenses + review git diff origin/<base>...HEAD>" < /dev/null`.
   **Low** tier may skip 6a: Codex plus your 6b checklist with `callsite-coverage.md`, and that
   checklist is the Claude review you record in node 4. Re-check the tier against the real diff
   first — it can only go up.
4. **Adjudicate once.** Verify each finding in the code before acting on it; where 6a and 6c disagree,
   read the disputed line and say which side was right. Then **6b brief compliance** as a checklist:
   DoD met, scope fences held, the repo's location and shape conventions. Green tests and a clean
   linter are preconditions, not review evidence.
   **Exit rule — only a contract `fail` blocks:** any CRITICAL, or an IMPORTANT at high confidence.
   Everything else goes under **Known / deferred** in the PR body, not into the loop.
   Record the orchestrator's verdict now, on the commit it reviewed — it carries forward to the fix
   commits: `review-receipt claude pass "<what 6a/6b found; fixes required: …>"`. **Pass means
   adjudicated**: the blockers become the worker's fix list and Codex verifies them in node 5. Record
   `fail` only when the work must be redesigned — a later round-1 pass on the redesign clears it (the
   gate lets the newest Claude verdict on the branch win).
5. **Fix (worker) → Codex-only delta review.** Send the worker ONE combined fix list. When it commits:
   Codex reviews `git diff <round-1 SHA>..HEAD` **and** the existing behaviour those fixes could break
   (fixes routinely break an adjacent mode the original change left alone) — no second reviewer-agent
   pass. Rerun `ci-preflight`.
   - **Fix-streak stop:** if round 2's findings were *created by* round 1's fixes, stop patching.
     Redesign (a redesign is a new round 1 with both reviewers) or surface to the user. Never a third
     patch round — a fix round that creates the next finding means redesign, not another patch.
   - A blocker still standing after round 2 → surface to the user: the finding, what was tried, why.
6. **Gate (code).** On the final commit: `ci-preflight` green, then
   `review-receipt codex pass "<what the last Codex pass found>"`. The pre-push hook needs: CI green
   on this SHA, Codex pass on this SHA, Claude pass on this SHA **or an ancestor** (or the same diff
   rebased). See `$ADW_HOME/docs/ci-gate.md`.
7. **Push + PR ready.** `git push` + `gh pr create --fill` (never draft). Remote CI confirms what
   `ci-preflight` showed; a remote-only red is a parity gap — fix the code AND the CI parity config.
8. **Review bot loop** (repos with a PR review bot). The bot (and any other review bot, e.g. Greptile)
   reviews every push. Treat their inline comments as findings: same exit rule, reply on every thread
   with the fix commit or why not.
   - **Approved for the current head** → node 9.
   - **It flags the PR high-risk** (risk capped, "human review required", or it says a human must
     review) → stop and surface to the user now; don't spend rounds it cannot approve.
   - **It comments** → triage; fix via the worker, Codex reviews the delta, `ci-preflight`, push,
     re-trigger the bot. **At most 3 bot rounds**, then surface to the user with what is open. The
     fix-streak stop applies here too.
   Repos without a bot: after push, surface to the user for approval — unless the repo's manifest entry
   sets `human_approve: false` (merge once green).
9. **Merge.** The bot approved the current head + CI green + `mergeStateStatus` `CLEAN` → squash-merge
   without asking, then watch the deploy if merging deploys. Anything else → the user decides
   (approves or forces it).

## When to stop early (the ONLY interrupts)
"Surface to the user" everywhere in this skill means: file an **`adw-ask` decision**
(`$ADW_HOME/docs/CLAUDE.snippet.md`) whose body holds the finding, what was tried, why it is stuck, each
option's consequence and your recommendation — then stop. When this `/adw` runs as a subagent of
`/adw-lead`, return the question to the lead instead; the lead files it.
Run standalone in a herdr tab, also declare state: `adw-status waiting "<CI / review bot on PR>"
--for <expected>` before ending a turn to wait, and `adw-status done|failed "<one line>"` at the end.
As a subagent of `/adw-lead`, don't — the lead declares for the tab.
- a genuine **business/product question** (as an `adw-ask` decision with lettered options), or a
  brief ambiguity code and house style can't settle;
- the **fix-streak stop** or a blocker after round 2 (node 5);
- the bot's **high-risk** flag or **3 rounds without approval** (node 8);
- anything outside the merge authority above.
Do NOT stop to confirm pushing, opening a PR, or marking it ready — those are the job.

## Large work — decompose, don't scale up
One `/adw` = one segment = one PR. For an epic use `/adw-lead`: plan in `$ADW_HOME/epics/<epic>/`,
Codex critiques the plan before any code, then `/adw` per segment — parallel workers when
independent. Avoid stacking segments on unmerged bases where the seams allow; stacked branches wait
for the base PR and then need a rebase.

## Reference
- Config: `$ADW_HOME/manifest.json` (repos: path, base_branch, human_approve, verify).
- Review contract + lenses: `$ADW_HOME/review/review-contract.md`, `$ADW_HOME/review/lenses/*.md`.
- Gate and local CI: `ci-preflight`, `review-receipt`, the pre-push hook — `$ADW_HOME/docs/ci-gate.md`.
- Worktrees: `cow-worktree` — `$ADW_HOME/docs/worktrees.md`.
