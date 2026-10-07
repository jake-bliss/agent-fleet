# The way of working

This is a method for running many coding agents at once without either babysitting them or letting
unreviewed code reach your main branch. It has three layers — a segment pipeline, an epic layer, and
a fleet layer — and one rule that runs through all of them: **you are asked only for decisions a
machine cannot default, and you are asked in one place.**

## The chain

```
                         you
                          │  answer adw-ask decisions on the board (desk or phone)
                          ▼
                     first mate            one tab: watches every tab, relays your word,
                          │                nudges stalled tabs once, keeps a digest
            ┌─────────────┼─────────────┐
            ▼             ▼             ▼
       epic lead     epic lead     (standalone tabs)     /adw-lead: plan, DAG, dispatch,
            │             │                              gates, adw-status lead <epic>
     ┌──────┴──────┐      │
     ▼             ▼      ▼
  /adw segment  /adw segment  …                          one segment → one PR
     │
     orchestrator (strongest model)
       ├── worker (cheaper model, own worktree) ── builds, fixes
       ├── reviewer agent (never saw the brief) ─┐ round 1, in parallel
       ├── Codex (a second vendor's model) ──────┘ then Codex alone on fixes
       └── ci-preflight (local or the CI box) ──── full PR CI before any push
```

## The actors

- **The orchestrator** — the strongest model you have. It plans, writes briefs, adjudicates reviews,
  pushes and works the PR. It does not write the code and does not read whole diffs; everything comes
  back to it as a conclusion, because every turn re-reads its context.
- **The worker** — a cheaper model, confined to one git worktree. Builds, runs targeted tests, commits.
  Never pushes.
- **Two reviewers from two vendors** — a fresh Claude reviewer agent that has not seen the brief, and
  Codex. They miss different things.
- **Local CI** — `ci-preflight` runs the repo's own PR workflows locally (or on a spare box), in the
  same order, before anything is pushed. It costs no tokens and is the most valuable single gate.
- **The PR review bot** — if your repo has one whose approval satisfies branch protection, it is the
  approver. If not, you are.
- **You** — product questions, the epic cut, replans, and any merge outside the bot's authority.

## The segment pipeline (`/adw`)

One brief becomes one PR. The orchestrator lints the brief against house style, sets a risk tier, and
creates a worktree off the *remote* default branch (`cow-worktree`, cheap on APFS). The worker builds.
The moment its commit lands, three things start at once: `ci-preflight`, the reviewer agent and Codex.
The orchestrator verifies every finding against the code, says which reviewer was right where they
disagree, and blocks only on a contract `fail` (a CRITICAL, or an IMPORTANT at high confidence);
everything else goes in the PR body as known/deferred. The worker fixes; Codex alone reviews the fix
delta. Receipts for both reviews and green CI on the exact SHA are recorded, and a pre-push hook
refuses the push without them. Then the PR opens, ready, and the review bot loop begins.

## The epic layer (`/adw-lead`)

Larger work is planned, not scaled up. An epic lead frames the goal, scouts the code with parallel
readers, and cuts the work into segments with dependencies — a DAG in `graph.json`. Codex critiques
the cut before you see it. Then it stops at **gate G2**: you approve or change the cut. After that it
dispatches ready segments (up to three in parallel by default), each as its own `/adw` in its own
subagent context, and pauses only at **Gmerge** (a PR the bot won't approve) and **Greplan** (a
segment found new work or a new dependency). It never replans silently. State lives on disk and
GitHub is the truth for merges, so an epic survives restarts and runs for days.

## The fleet layer

- **The ADW Board** — a local web page over your herdr tabs: what each tab is doing, which are
  waiting, stalled or done, every epic's DAG, and the open decisions. Reachable from your phone over
  Tailscale if you want.
- **`adw-ask`** — the decision queue. An agent that needs you files a decision with context, evidence,
  options and a recommendation, then ends its turn. You answer on the board; the answer is typed into
  that agent's pane. Nothing waits on you in a terminal you aren't looking at.
- **`adw-status`** — each tab declares `waiting … --for 30m`, `done` or `failed`, so the board can
  tell a tab waiting on CI from one that died. Epic leads register with `adw-status lead <epic>`.
- **The first mate** — one long-lived tab that watches everything, relays your instructions into the
  right tab, nudges a stalled tab once, routes epic matters to that epic's lead, and keeps a short
  digest (needs you / trouble / ready / under way).
- **`/continue <id>`** — jump straight to one decision, tab or epic.

See `board.md`.

## The review economics

- **Why two models.** The orchestrator reviewing work built to its own brief is the same mental model
  twice; a second Claude helps, a different vendor's model helps more. Round 1 uses both.
- **Why Codex alone on fixes.** A fix delta is small and the expensive context is already
  adjudicated; a second full round costs a lot and finds little.
- **The fix-streak stop.** If round 2's findings were created by round 1's fixes, the design is
  wrong. Redesign (a new round 1) or ask the human — never a third patch round.
- **At most 3 review-bot rounds.** Past that, rounds produce churn, not approval. A bot that flags
  the PR as needing a human goes to the human at once.
- **CI before push.** Remote CI and bots are shared and rate-limited; they confirm, they don't
  discover.

## What you do

Approve epic cuts. Answer decisions on the board. Approve or force merges the bot won't make. Talk to
the first mate when you want something changed across the fleet. Run `/clear` when an agent tells you
its context is due. That's it — pushing, opening PRs, marking them ready and merging approved work are
the agents' job, not questions for you.
