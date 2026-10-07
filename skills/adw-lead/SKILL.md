---
name: adw-lead
description: >-
  Conduct large projects by planning an epic, cutting it into a dependency DAG of segment briefs, and dispatching per-segment /adw runs as dependencies clear while keeping the user at the decision gates. Semi-automatically drives ready segments to merge, pausing for decomposition approval, replans, and any PR the review bot will not approve. Use for multi-segment refactors or phased features. Triggers on "run adw-lead", "/adw-lead", "orchestrate this epic", and "adw-lead <epic>". Use /adw for one scoped segment.
---

# ADW-LEAD — the epic conductor

One tier above `/adw`. `/adw` builds ONE segment to a merged PR (local CI green, reviewer + Codex
reviewed, approved by the PR review bot or surfaced). `/adw-lead` plans the whole epic, maintains the
dependency DAG, and dispatches `/adw` runs as segments become ready — with the user at the gates.
Engine and state in `$ADW_HOME` (default `~/.claude/adw`). Driver: `adw-lead-state`.

**Invocation:** `/adw-lead <epic>` (creates it if new, else reconciles and advances it).

## Actors
- **The user** — the gates: approve the cut, decide replans, and approve (or force) any PR the review
  bot will not approve. Nothing else pauses.
- **The orchestrator (this skill)** — conduct: scout, decompose, dispatch, integrate, surface decisions.
- **Codex** — critiques the plan and the cut before any code (read-only), reviews inside each `/adw`.
- **/adw per segment** — worker builds, reviewer + Codex review, local CI, review bot loop.
- **Code (`adw-lead-state`)** — epic state, PR polling, ready-set math. Zero tokens.

## Autonomy contract
**Semi-auto.** Auto-dispatch ready segments (up to `max_parallel`) and run each `/adw` to its PR gate
without asking. **Hard-pause ONLY for:**
1. **Decomposition** — the segment cut + DAG, before ANY building. [G2]
2. **Merges outside the authority** — a segment PR merges on its own only when the review bot approved
   its current head (CI green, `CLEAN`). Anything the bot flags high-risk or won't approve in 3
   rounds — and every PR in a repo with no bot — comes to the user. [Gmerge]
3. **Replans** — when a segment surfaces new work, a spike, or a scope or dependency change. [Greplan]
Parallelism is auto (`max_parallel` in `graph.json`, default 3) — proceed without asking; the user can
override the number anytime.

## How the user is asked
Every gate and every decision goes to the user as an **`adw-ask` decision**, filed with `--epic <epic>`
and lettered options. The body must let them decide from the ADW Board on a phone without opening this
tab: what was found, the evidence (PRs, file:line, numbers), each option's consequence, your
recommendation and why. Then end the turn; the answer arrives in this pane as
`[adw-ask <id> answered] …` — log it to `decisions.md` and resume. Not AskUserQuestion: the board shows
a dialog only as its last screen lines, and it cannot be answered from the phone. Outside herdr (no
`HERDR_PANE_ID`) fall back to AskUserQuestion. Keep no separate pending-questions file — open questions
live in `adw-ask list`.

**Declare state with `adw-status`.** Before ending any turn that waits — segments building, PRs with
the review bot, the CI queue, an open `adw-ask` — run
`adw-status waiting "<what; segment ids / PRs>" --for <how long until you'd expect news>`. On epic
done or an unrecoverable stop, `adw-status done|failed "<one line>"`. This is what lets the board tell
a conductor waiting on the review bot apart from one whose tab died with its segment subagents in it.

## Chain of command
The user → **first mate** (one tab watching the whole fleet) → **you, the epic lead** → your crew
(segment subagents, plus any other tab working this epic). The first mate talks to you, not to your
crew, about anything in this epic; you own what happens inside it.
- **Register as lead** at the start of every invocation (and again after a restart or `/clear`):
  `adw-status lead <epic>`. The board then knows this tab leads the epic, and the first mate routes
  epic matters here. If the tab dies, the board flags the epic as leaderless.
- **Messages starting `[first mate]`** come in two kinds. A relay ("the user says: …") carries the
  user's authority — act on it as if they had typed it here. A crew report ("your crew w7:p4 stopped
  40m after …") is yours to handle: `herdr agent read` that tab, then nudge it, take its work back, or
  file an `adw-ask` if only the user can unstick it. Reply in one line on what you did; the first mate
  reads your tab, not your files.
- Decisions still go straight to the user via `adw-ask`, never through the first mate.

## The conductor loop

Each `/adw-lead <epic>` invocation reconciles state, then advances. The first run walks
Frame→Decompose; later runs resume mid-DAG.

### 1. Frame  (new epic only)
`adw-lead-state init <epic> <repo-key> "<goal>"`. Fill `plan.md` success criteria and scope fences
from the goal. No gate — but confirm the restated goal in one line before scouting.

### 2. Scout
Fan out parallel readers (Explore / general-purpose agents) over the affected code and systems.
Produce a landscape: the seams, the risky couplings, what already exists.

### 3. Decompose  →  **GATE G2 (hard)**
Cut the epic into segments. For each: an id, an `/adw`-ready brief in `epics/<epic>/briefs/`
(guardrails, scope fences, DoD; house-style-linted per `/adw` F5), and its `deps` (segment ids it
needs merged first). Build the DAG in `graph.json`
(`segments:[{id,brief,deps,status:"pending",pr:null}]`). Make the cut **adversarial**: you propose,
a Codex pass critiques it (missing deps, segments too big to review, wrong seams) — run that critique
read-only via `codex exec -C "<repo>" -s read-only "<critique prompt>" < /dev/null`.
**Then STOP.** File the cut + DAG + the parallel/sequential shape as an `adw-ask` decision (options:
approve as cut / change it — the user's note carries any reorder, drop/add, resize) and end the turn.
Log the decision to `decisions.md`. No dispatch before approval.
Sizing rule: a segment must yield a PR the user can actually review; an epic of a few PRs is normal.

### 4. Dispatch  (semi-auto)
`adw-lead-state reconcile <epic>` → updates statuses and marks `ready` any pending segment whose deps
are all `merged`. `adw-lead-state ready <epic>` → the dispatchable ids (respects `max_parallel` minus
in-flight). For each: set status `building`, then run **`/adw <repo> <brief>` in its own context** —
a background `general-purpose` Agent per segment on the strongest model (it orchestrates and
adjudicates; its own builders are workers), all launched in ONE message, each told to return only:
PR number, worktree path, risk tier, review verdict + Known/deferred list, and any stop-early question
verbatim. Never run a segment's `/adw` inline in the lead session: every build log and review report
would then stay in the lead's context and be re-read on every later turn of a days-long epic. A
subagent cannot ask the user — when one returns a stop-early question, the lead files it as an
`adw-ask` decision (one per question) and resumes that agent with the answer via SendMessage. Record
the opened PR number and worktree back into `graph.json`; `adw-lead-state reconcile` tracks it
thereafter. Independent segments dispatch in parallel; dependent ones wait.

### 5. Integrate  →  **GATE Gmerge (hard)** + **GATE Greplan (hard)**
After a dispatch pass, `adw-lead-state status <epic>` and present: what's building, what's `pr-open`
and **awaiting the user** (bot high-risk, out of rounds, or no bot — each such PR gets its own
`adw-ask` decision: merge / hold / force, with the open findings in the body), what merged on its own,
what's blocked. The next `/adw-lead` invocation reconciles — a merge flips a segment to `merged` and
unlocks its dependents (they become `ready`), and the loop continues.
If any segment surfaces a surprise (a lens finding implying new work, a Codex blocker, a newly
discovered dependency, a scope change) → **STOP, log it, file an `adw-ask` decision** before altering
the DAG or briefs. Never silently replan.

### 6. Done
DAG fully `merged` + `plan.md` success criteria met → file an `adw-ask` decision "mark <epic>
complete?" whose body is the summary (what shipped, PRs, anything deferred). The user marks it
complete on the ADW Board (`COMPLETE.json` in the epic dir); never write that marker yourself.

## State — persistent, survives sessions
```
$ADW_HOME/epics/<epic>/
  plan.md        goal, success criteria, scope fences
  graph.json     segments + deps + status + pr  (the DAG — source of truth)
  briefs/        one /adw-ready brief per segment
  decisions.md   append every gate decision: what, choice, why, date
```
Status flow: `pending → ready → building → pr-open → merged` (`blocked` on failure/closed).
Because epics span days, always `adw-status lead <epic>` and `adw-lead-state reconcile <epic>` FIRST
each invocation — GitHub is the truth for PR and merge state; `graph.json` is the plan.
Keep `graph.json` true between invocations too: reconcile after every merge, and when a decision
changes a segment (parked, dropped, option chosen, unblocked) update its `status` and `note` in the
same turn you log it to `decisions.md`. The ADW Board reads `graph.json`, so a stale `blocked` shows
the user a decision they already made.

## Inherit /adw's hardening
Every dispatched segment run is a full `/adw` and carries its hardening (worktree-only, remote base,
private test DBs, brief lint, independent reviewers) and its review sizing: risk tier, the
contract-`fail` exit rule, one reviewer + Codex round then Codex-only fix review, the fix-streak stop,
and at most 3 review-bot rounds. `/adw-lead` adds nothing that bypasses them.

## When to reach for /adw-lead
- **Epic** — many segments, real dependency structure (phased feature, cross-seam refactor). Yes.
- **Single segment** — one brief → one PR. Use `/adw` directly.
- **Quick / exploratory** — neither. Work normally.
```
EPIC → /adw-lead → decompose[G2] → dispatch /adw × N → merges (bot or the user[G]) → replan[G] → done
```
