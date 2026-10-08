# /adw — rationale

Every rule here is stated in `SKILL.md`; this file is the why.

## Actors
- The orchestrator does not read whole diffs or write code because every turn re-reads its context:
  a file dump costs its size × every remaining turn.
- Codex costs no Claude usage, so it carries every later review and makes a good builder.
- Local CI (`ci-preflight`) costs no model usage at all. The fast tier exists for repos whose full
  suite is too slow to run locally on every push; GitHub CI then runs it against a draft PR.

## F2 — never the main checkout
A repo's main checkout may be detached or carry a large pile of unrelated dirty files.

## F4 — private DBs, targeted tests in workers
A worker's local full-suite run can hang for hours, and shared test databases make parallel agents
deadlock or corrupt each other's state.

## F5 — lint briefs
Briefs carry latent house-style bugs; fixing them before the build is cheaper than in review.

## F6 — remote base
Local base refs go stale silently.

## F7 — independent reviewers
The orchestrator reviewing work built to its own brief applies the same mental model twice; whatever
the brief failed to anticipate is invisible to both passes.

## Codex as builder
A worker build costs a lot of Claude tokens; Codex costs none and is often spare capacity. Its
workspace-write sandbox blocks `.git`, so the orchestrator (or a driver script) commits.

## F8 — the size cap
Review-bot rounds and time-to-merge climb steeply with PR size; past roughly a thousand changed lines
a PR typically needs several bot rounds and fix streaks become common. Under the cap, most PRs pass
in zero or one round.

## Node 2 — worker brief
- Targeted runs alone keep missing the repo's guard/census tests and the callers of changed methods.
- Workers are a large share of total spend because each turn re-reads their context — hence the
  context-hygiene list.

## Node 5 — Codex-only delta review
- The delta review must cover existing behaviour the fixes could break: fixes routinely break an
  adjacent mode the original change left alone.
- The `-o` output file keeps Codex's full output out of the orchestrator's context.
- Fix-streak stop: a fix round that creates the next finding means redesign, not another patch.

## Node 7 — draft-first push (fast tier)
GitHub CI runs the full suite on the draft. Review bots usually skip drafts, so a red there costs a
push, not a review round. Full-tier repos: remote CI only confirms the local gate.

## Node 8 — review bot loop
Don't spend rounds on a PR the bot flags high-risk — it cannot approve it. A bot that re-reviews on
every push to a ready PR is why non-trivial fixes go back to draft first.

## Large work
Stacked branches wait for the base PR and then need a rebase.
