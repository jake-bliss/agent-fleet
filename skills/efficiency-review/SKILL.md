---
name: efficiency-review
description: Weekly review of Claude Code spend vs output (cost per merged PR, context bloat, stale models, review cost) with one or two concrete changes to try next. Use on /efficiency-review or "how did performance / token efficiency look".
disable-model-invocation: true
---

# Efficiency review

Goal: cheaper merged PRs without slowing the fleet. Spend is context × turns (re-reads ≈ 60%), not output.

## 1. Measure (one command, no file dumps)

```bash
efficiency-report --days 7 --by-day --record   # daily table + appends history
efficiency-report --day <worst day> --top 15    # drill into one day
efficiency-report --hours 4 --compare           # did a change just land?
```

Per-epic delivery (run on any epic marked complete since the last review; the board's Complete button runs it
automatically): `adw-epic-report <epic> [--write]`, `adw-epic-report --history` for the table
(active/held days, segments, PRs, PR cycle hours, review-bot rounds, user asks, ~$ and $/PR from lead sessions +
subagents; Codex not counted). Compare new epics against that table, not against each other's raw totals.

History lives in `$ADW_HOME/efficiency/history.jsonl` (one line per recorded day). Compare against it
before drawing a trend; dedupe by `start` when the same day was recorded twice. Prices are list-price
equivalents (plan billing differs); use them for ratios, not invoices. The weekly plan limit is on the
first mate's status line (`week limit N%`), so read it there.

## 2. Read the numbers in this order

| Metric | Healthy | If not |
|---|---|---|
| `$/PR` (yours, merged) | trending down | find the top transcripts that day |
| `stale$` (models other than the pinned ones) | 0 | an alias drifted: check `~/.claude/agents/*.md` `model:` and settings env |
| `>cap$` (turns over 250k ctx) | falling as leads are relaunched | the tabs doing it lack `spawned-settings.json` |
| `maxctx` | ≤ ~300k | a lead or worker never compacts: relaunch it (first mate: prep → /exit → herdr agent start --settings) |
| re-read share | <60% | long sessions: /clear due, lead refresh |
| subagent share | ~70% normal | Opus reviewer share >30% → are diffs too big (segment cap ~800 lines)? |

Then the top-transcripts list: name each expensive one (tab title via `herdr agent list`, session id → pane),
and say what it was doing and whether the spend bought a PR.

## 3. Output

- The daily table, plus three lines: what got better, what got worse, and the single biggest leak.
- One or two changes to try, each reversible and measurable next week. Record them in
  `$ADW_HOME/efficiency/changes.md` with the date and the metric you expect to move.
- Never change review gates, safety rules or merge rules for cost. Raise those with the user instead.

## Current setup (verify before relying on it)

- Workers are pinned to a full model id in `~/.claude/agents/worker.md`; pin the model aliases in `settings.json` env too.
- Board-launched and first-mate-launched tabs get an auto-compact window via `--settings $ADW_HOME/spawned-settings.json`.
  Your own tabs and the first mate don't have this cap.
- Leads started before the cap existed lack it until they are relaunched; check `>cap$`.
