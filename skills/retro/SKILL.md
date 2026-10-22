---
name: retro
description: Run a retrospective on a coding session, producing changes to the agent's ENVIRONMENT (files, checks, scripts), not resolutions. Use on /retro or "post-mortem" / "what should we change after that session". Not for code review (/code-review) or summaries.
disable-model-invocation: true
---

# Retro — fix the environment, not the agent's resolve

Propose improvements to the coding agent's **environment** so future runs go better. **Never "I will be more careful"**: the next session inherits only files. A finding that does not name a file to change is not a finding.

## Steps

1. **[LOCAL] Establish the session's facts, then distrust them.** Read primary sources for the session named (default: current; logs under `~/.claude/projects/*/`). A transcript holds the agent's own confident assertions. Before building a finding on any claim, re-verify it (read the file, run the command, query the data). If you cannot verify, say so in the finding rather than dropping or asserting it.

2. **Measure the steering surfaces.** Line counts and byte sizes of `~/.claude/CLAUDE.md`, the project `CLAUDE.md`, `MEMORY.md`, and any lens/contract used. Context limits truncate these silently (a 268-line index whose tail never loads is a dead memory system). Check for a truncation warning and check sizes yourself.

3. **Look for candidates.** Each has a trigger; if it did not fire say "no finding" — a padded retro trains the user to skim.
   - **Navigation** — trigger: real time spent hunting for information. Would a pointer help?
   - **Automated checks** — trigger: a mistake a check could have caught, or a missing guardrail. Look at the repo's own check command first (`just`, `package.json`, CI workflow, `ci-preflight`). Also checks that report the wrong thing (red = "tooling broken", green = "job skipped") are worse than no gate.
   - **Coding standards** — trigger: the reviewer failed to catch something. Mechanical violations (syntactic patterns, banned APIs, import shapes, file locations, a comment naming a constant that no longer resolves) get deterministic checks; prose standards only for judgment calls.
   - **Steering-file hygiene** — trigger: file is large. Instructions in `CLAUDE.md`/`AGENTS.md` that belong in a standard or a check.
   - **Tool economy** — trigger: expensive/repeated calls. A reminder that keeps being forgotten is a script never written.
   - **No-ops** — trigger: steering files large. Instructions that don't change behaviour: delete.
   - **Information access** — trigger: crucial info unavailable. Say plainly when the fix is organisational.
   - **[LOCAL] Unverified assertion** — trigger: any claim turned out wrong or a reviewer overturned a conclusion. What was stated as fact without checking, and what would have forced the check? Remedy is usually a test, a probe helper that prints the real shape before filtering, or a rule that the claim cite its source line.

4. **Rank by expected future cost**, not annoyance (a silent degradation affecting every session outranks a loud caught one-off). State the ranking basis.

5. **Present** each candidate with: the trigger that fired, the exact file to change, and the change. Then stop; do not apply unless the user says so, but have them ready to apply.

Read `references/examples-and-provenance.md` for a good and a bad example finding and provenance.
