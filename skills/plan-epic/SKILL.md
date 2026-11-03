---
name: plan-epic
description: Plan an epic with the owner in one tab before any lead runs: sharpen the ask, gather evidence, write falsifiable criteria, run the prerequisite checks, forecast the cut, get a Codex critique, then hand the approved plan to the first mate. Triggers on "/plan-epic", "plan an epic", "plan this with me", "epic prep". One-PR work goes to /adw; running an approved epic goes to /adw-lead.
---

# Plan an epic

You are in a tab with the owner. The output is a plan pack in `$ADW_HOME/epics/<slug>/` (default `~/.claude/adw`) that
a lead runs without re-framing it. You write no code, cut no worktrees or branches, open no PRs, write no briefs and
start no lead. On the owner's approval you hand off; the first mate starts the lead.

## 0. Where and what
- `adw-lead-state` missing: stop and say the fleet is not installed here.
- The argument is an idea, or the slug of an epic in planning (`PLANNING.json`, no `READY.json`): resume it
  from its `plan.md` and `decisions.md`, then say in one line where it stands.
- One PR's worth of work (about 800 changed lines or fewer, one repo, no ordering) is not an epic: say so and suggest
  `/adw <repo> <brief>`. Stop unless the owner still wants an epic.

## 1. Sharpen the ask
Restate the goal in your own words in two lines. Then, briefly:
- what a top engineer would do here that the ask leaves out;
- the rawest evidence to start from (data rows, logs, the code, the requester's own words), not summaries of it;
- what, outside you and the owner, will show the result is right (a query, a test, a stakeholder confirming).
Ask the owner only what you cannot find out yourself; answer the rest from code and data.

## 2. Scaffold
- Overlap: `adw-lead-state fleet -` and `ls "$ADW_HOME/epics" | grep -i <words>`. A live or planned epic
  already covering it: show the owner, don't start a second one.
- Repo key from `$ADW_HOME/manifest.json`; check its path is the repo you mean
  (`git -C <path> remote get-url origin`).
- Slug: lowercase, `a-z0-9-`, at most 32 characters (it becomes the lead's tab name).
- `ADW_BY=planner adw-lead-state init <slug> <repo-key> "<goal>" --planning`.

## 3. Ground
Delegate wide reads (`Explore` with `model: "haiku"`, or general-purpose) and keep the conclusions. Production reads follow
your read-only rules. Save every measurement under `evidence/` with the command that re-runs it and the date.

## 4. Write `plan.md`
Fill these sections; replace the `(fill at the frame gate)` placeholder.
- **Goal**, and **Problem and evidence** (numbers, `file:line`, ticket and thread links).
- **Success criteria**: each one can be checked true or false and names the check (command, test, query), the job or
  code that writes the value it checks, and how that writer's guards are passed. "Works well" is not a criterion.
- **Scope fences**, and follow-ups explicitly left out.
- **Prerequisites**, each run now with the result pasted: repo path and default branch; tools on the machine the lead
  and workers will use; access (check that keys exist, never print them); open PRs or epics touching the same files
  (`gh pr list -R <repo> --search <path>`).
- **Risks**: for each risky change, the one fact it is safe because of, and how far that fact is proven:
  said, pointed at the line, walked through, ran a read-only probe, reproduced. Push the load-bearing ones to
  "ran" now. Name the lenses the work touches (`$ADW_HOME/review/lenses/*.md`).
- **Shape** (a forecast; the lead makes the real cut at its G2 gate):
  - Sketch two structurally different cuts (for example by layer or by vertical slice), pick one, and say why.
  - Riskiest unknown first. Verification and baseline capture come before features.
  - Each segment lands on its own and ends in a checkable state.
  - List the segments with forecast changed lines (800 each at most; over 600 gets split) and their dependencies.
- **Open questions**: the owner is here, so ask now. Log each answer in `decisions.md`. Anything still open at
  handoff becomes an `adw-ask --epic <slug>`.

## 5. Critique
`codex-review plan <repo> "$ADW_HOME/epics/<slug>/plan.md"`. Adjudicate each finding, never relay it. A
finding about current code survives only when you probe the tree and it shows. Fold the survivors into
`plan.md` and tell the owner which ones changed the plan.

## 6. Approve and hand off
Show the owner: goal, criteria, forecast (segments, lines), top three risks with their proof level, open asks.
Change the plan until they approve it in plain words ("approve", "ship it", "hand it off"). Then:

```bash
ADW_BY=owner adw-lead-state handoff <slug> <segments> <lines> "<one-line summary>"
adw-status done "planned <slug>; handed to the first mate"
```

`READY.json` puts the epic in the board's Ready state. The first mate starts its lead, which skips Frame, scouts,
and brings the real cut to the owner as its G2 ask. Tell the owner the tab can be closed.

## Never
- Hand off without the owner's approval in this tab, or start the lead yourself.
- Edit code, create worktrees or branches, or write segment briefs or `graph.json` segments (the lead's job).
- Print secrets; make production writes of any kind.
