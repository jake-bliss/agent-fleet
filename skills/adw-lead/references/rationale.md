# /adw-lead — rationale

Every rule here is stated in `SKILL.md`; this file is the why.

## Position
One tier above `/adw`: `/adw` builds ONE segment to a merged PR (CI green, reviewer + Codex reviewed,
approved by the review bot or surfaced); `/adw-lead` plans the epic, maintains the DAG, dispatches
`/adw` runs. `adw-lead-state` is plain code and costs no tokens.

```
EPIC → /adw-lead → decompose[G2] → dispatch /adw × N → merges (bot or the user[G]) → replan[G] → done
```

## Defaults (`--default-after`)
For reversible calls inside an approved scope, the user usually takes the recommendation. A default
lets the work continue overnight instead of idling on an answer that was predictable, while the user
can still override it before it applies.

## Why not AskUserQuestion
The board shows a terminal dialog only as its last screen lines, and it cannot be answered from a phone.

## adw-status
Lets the board tell a conductor waiting on the review bot apart from one whose tab died with its
segment subagents inside it.

## Lead registration
The board then knows this tab leads the epic and the first mate routes epic matters here. If the tab
dies, the board flags the epic as leaderless. Re-registering re-routes open and unlogged decisions
from the predecessor session.

## Crew reports
The first mate reads your tab, not your files — hence the one-line reply.

## Short-lived conductor
Context is re-read every turn, so a conductor left running for days becomes one of the largest
single costs in the fleet. Everything durable is on disk (`graph.json`, `decisions.md`, open asks),
so a refresh loses nothing.

## Size rule
Review-bot rounds and merge time climb steeply past ~800 changed lines (see /adw
`references/rationale.md`, F8).

## builder field
Recording `builder` per segment lets you compare Codex-built and Claude-built segments later (bot
rounds per size band, Claude tokens per PR). Codex is often spare capacity.

## Dispatch out-of-process
Run inline, every build log and review report would stay in the lead's context and be re-read on
every later turn of a days-long epic. The lead files subagent questions because a subagent cannot ask
the user.

## graph.json freshness
The board reads `graph.json`, so a stale `blocked` shows the user a decision they already made. Epics
span days, so reconcile first every invocation.
