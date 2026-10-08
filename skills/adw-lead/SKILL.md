---
name: adw-lead
description: "Conduct an epic: plan it, cut it into a dependency DAG of ≤800-line segment briefs, dispatch /adw per segment as deps clear, with the user at the gates (the cut, replans, merges the review bot won't approve). Triggers on \"/adw-lead\", \"run adw-lead\", \"orchestrate this epic\", \"adw-lead <epic>\". One segment → /adw."
---

# ADW-LEAD — the epic conductor

`/adw-lead <epic>` creates the epic if new, else reconciles + advances it. State in `$ADW_HOME`
(default `~/.claude/adw`); driver `adw-lead-state` (state, PR polling, ready-set math, zero tokens).
The why behind these rules: `references/rationale.md` — read it when a rule seems not to fit.

**Actors.** The user: the gates. You (the strongest model): scout, decompose, dispatch, integrate,
surface decisions. Codex: read-only critique of plan and cut; reviews inside each `/adw`. `/adw` per
segment.

## Autonomy — semi-auto
Auto-dispatch ready segments (up to `max_parallel` in `graph.json`, default 3; the user may override)
and run each `/adw` to its PR gate without asking. Hard-pause ONLY for:
1. **G2** — the segment cut + DAG, before ANY building.
2. **Gmerge** — a segment PR merges on its own only when the review bot approved its current head (CI
   green, `CLEAN`). Bot high-risk, unapproved after 3 rounds, or a repo with no bot → the user.
3. **Greplan** — a segment surfaces new work, a spike, or a scope or dependency change.

## Asking the user
Every gate and decision → an **`adw-ask`** (`$ADW_HOME/docs/CLAUDE.snippet.md`), `--epic <epic>`,
lettered options. The body lets them decide from the board on a phone: what was found, evidence (PRs,
file:line, numbers), each option's consequence, your recommendation and why. End the turn; the answer
arrives as `[adw-ask <id> answered] …` — log it to `decisions.md` **with the id** (unlogged ids are
replayed after a refresh), then resume.
**Defaults:** a call that is reversible, inside one segment's approved scope, with a clear
recommendation (technical approach, library choice, redesign after a fix-streak stop) → file with
`--recommend <key> --default-after 4h` (overnight `8h`) and keep working on what doesn't depend on it;
unanswered, the recommendation is applied and arrives as a normal answer. **Never** a default on G2,
Gmerge, Greplan (any change to the DAG or a segment's scope, order or split), product/scope questions,
anything irreversible, or anything touching money, auth or customer data. The CLI enforces only a
30-minute minimum — this list is yours to keep.
Not AskUserQuestion; outside herdr (no `HERDR_PANE_ID`) use AskUserQuestion instead. Keep no separate
pending-questions file; open questions live in `adw-ask list`.
**adw-status:** before ending any turn that waits (segments building, PRs with the review bot, the CI
queue, an open ask): `adw-status waiting "<what; segment ids / PRs>" --for <until news expected>`.
Epic done or unrecoverable stop: `adw-status done|failed "<one line>"`.

## Chain of command
The user → **first mate** → **you, the epic lead** → crew (segment subagents + any other tab on this
epic). The first mate talks to you, not your crew, about this epic; you own what happens inside it.
- **Register** at the start of every invocation (and after a restart or `/clear`): `adw-status lead <epic>`.
- **`[first mate]` messages**: a relay ("the user says: …") carries the user's authority — act as if
  they typed it. A crew report is yours: `herdr agent read` that tab, then nudge it, take its work
  back, or file an `adw-ask` if only the user can unstick it. Reply in one line with what you did.
- Decisions go straight to the user via `adw-ask`, never through the first mate.
- A segment returned **incomplete** (size cap) is not pushed; splitting the remainder is a Greplan —
  file it, don't improvise the new segment.
- **Stay short-lived.** Whenever this tab has **nothing running in-process** (no background segment
  agents, no Monitor you need) and you end a turn to wait (review bot, CI, an ask, a merge):
  `adw-status waiting "<what>" --for <time> --refreshable`. Past the board's refresh threshold
  (`ADW_LEAD_REFRESH_AT`, default 150k context tokens) the first mate may `/clear` this tab and start
  `/adw-lead <epic>` fresh; you resume by reconciling from disk. **Never** mark refreshable while
  segment agents run here — clearing kills them.
- Don't read subagent transcripts or `.output` files; the returned summary is the result.

## The loop
Every invocation FIRST: `adw-status lead <epic>`, then `adw-lead-state reconcile <epic>` (GitHub is
truth for PR/merge state; `graph.json` is the plan). New epics walk 1→3; later runs resume mid-DAG.
1. **Frame** (new only) — `adw-lead-state init <epic> <repo-key> "<goal>"`; fill `plan.md` success
   criteria + scope fences; confirm the restated goal in one line before scouting.
2. **Scout** — parallel Explore/general-purpose readers over the affected code → landscape: seams,
   risky couplings, what exists.
3. **Decompose → G2.** Per segment: id, an `/adw`-ready brief in `epics/<epic>/briefs/` (guardrails,
   scope fences, DoD; F5-linted), `deps`. DAG in `graph.json`
   (`segments:[{id,brief,deps,status:"pending",pr:null}]`).
   - **≤ ~800 changed lines** per segment (/adw F8); cut more, smaller; state each brief's expected
     size. No stack deeper than one unless each base merges before its dependent starts.
   - Record `"builder": "claude" | "codex"` per segment; prefer Codex for mechanical, low-risk
     segments; overnight Codex follows `$ADW_HOME/docs/codex-overnight.md`.
   - Adversarial cut: Codex critiques (missing deps, too-big segments, wrong seams) via
     `codex exec -C "<repo>" -s read-only "<critique prompt>" < /dev/null`.
   - **STOP:** file cut + DAG + parallel/sequential shape as an `adw-ask` (approve as cut / change it
     — the user's note carries reorder, drop/add, resize), end the turn, log to `decisions.md`. No
     dispatch before approval.
4. **Dispatch** — `adw-lead-state reconcile <epic>` marks `ready` pending segments whose deps are all
   `merged`; `adw-lead-state ready <epic>` lists dispatchable ids (`max_parallel` minus in-flight). For
   each: set status `building` and `builder`, then run `/adw <repo> <brief>` as a background
   `general-purpose` Agent on the strongest model, all in ONE message; say in the prompt whether its
   builder is a worker or Codex. Each returns only: PR number, worktree path, risk tier, review verdict
   + Known/deferred, any stop-early question verbatim. **Never** run a segment's `/adw` inline in the
   lead session. A subagent can't ask the user: you file each of its questions as an `adw-ask` and
   resume it with the answer via `SendMessage`. Write PR number + worktree into `graph.json`
   (reconcile tracks it after). Dependent segments wait.
5. **Integrate → Gmerge + Greplan** — `adw-lead-state status <epic>`; present: building; `pr-open`
   **awaiting the user** (bot high-risk, out of rounds, or no bot — each its own `adw-ask`: merge /
   hold / force, with the open findings); merged on its own; blocked. Any surprise (lens finding
   implying new work, Codex blocker, new dependency, scope change) → **STOP, log it, file an
   `adw-ask`** before altering the DAG or briefs. Never silently replan.
6. **Done** — DAG all `merged` + `plan.md` criteria met → `adw-ask` "mark <epic> complete?" with the
   summary (shipped, PRs, deferred). The user marks it on the board (`COMPLETE.json`); never write it
   yourself.

## State — `$ADW_HOME/epics/<epic>/`
`plan.md` (goal, criteria, fences) · `graph.json` (the DAG, source of truth) · `briefs/` ·
`decisions.md` (every gate decision: what, choice, why, date, ask id).
Status: `pending → ready → building → pr-open → merged` (`blocked` on failure/closed). Keep
`graph.json` true between invocations: `adw-lead-state reconcile` after every merge; when a decision
changes a segment (parked, dropped, option chosen, unblocked) update its `status` and `note` in the
same turn you log it — the board reads `graph.json`.

## Inherit /adw
Every segment is a full `/adw`: worktree-only, remote base, private test DBs, brief lint, independent
reviewers; risk tier, contract-`fail` exit rule, one reviewer + Codex round then Codex-only fix
review, fix-streak stop, ≤ 3 review-bot rounds. Nothing here bypasses them. Single segment → `/adw`;
quick/exploratory work → neither.
