# Codex overnight builds: how an epic lead writes the goal prompt

For `/adw-lead` conductors. Read it before you hand Codex unattended build work.

Codex tokens are often spare capacity, while a worker build costs real Claude usage and every
review-bot round costs orchestrator turns. Codex-built PRs tend to need more bot rounds than
worker-built ones when they are large, stacked or touch risky domains — so use Codex to build, under
these rules, and record `builder` per segment so you can compare the two on your own repos.

## 1. When to use Codex overnight

Use it for settled briefs (decisions logged in `decisions.md`, no open product question), well-fenced
work (a service plus tests, consumers of a merged API, docs/OpenAPI, test-only harnesses), and
segments that reach "branch ready for review" without anything merging first. Not for: anything that
needs the user mid-run (the night is lost), a base that is unmerged or failing review (§2), or any
production operation (prod tasks, deploys, infrastructure or environment values).

**High-risk tier: payments, pricing, auth/tokens, migrations.** Codex may build these, but the brief
must also:
- [ ] **feature-flagged / gated paths:** land a gate-off parity harness first, as a test-only commit.
      It exercises each touched entry point with the base branch's exact options and with the
      branch's, and asserts identical output and queries. Without it, review rounds end up doing the
      harness's job and each fix introduces a new gate-off regression. Also: never re-record a frozen
      contract fixture; never coerce nil to 0; refuse only before payment, never on a retry of a
      captured payment;
- [ ] **auth/token/sandbox:** name the lenses (e.g. `cross-tenant-isolation`). Require that every kill
      switch is proven operable at runtime by a test that flips it — a build-time constant that the
      deploy never plumbs through is not a kill switch. Put "HIGH RISK" at the top of the PR body;
- [ ] **migrations:** apply the `migration-lock-safety` lens. On a version collision, restamp to a
      later timestamp. Regenerate schema dumps by loading the base branch's schema into a private DB,
      never by hand-merging. **No migration that the brief does not name**;
- [ ] budget the user's time at decomposition: one critical from the review bot usually means a human
      must approve.

## 2. Cutting the night's work

- [ ] **≤ ~800 changed lines per segment** (`git diff --stat origin/<base>...HEAD`, tests included).
      Split anything bigger before writing the prompt; oversized slices stop on fix streaks or draw
      criticals in round 1.
- [ ] **No stack deeper than 1 unless its base merges first.** Chain only when the base merges before
      the child starts; otherwise build the segments in parallel, each off `origin/<base>`.
- [ ] **Never build on a stopped base.** Children of an unsafe base cannot merge even when they are
      approved. Put a stop condition in the prompt: if a base segment stops, every dependent stops too.
- [ ] **Parallelism:** ≤3 segments a night, each with its own worktree and private test DB. If your CI
      box has a single slot, give the preflights a fixed order rather than letting them queue and time
      out.
- [ ] Per segment: own worktree, branch `codex/<epic>-<seg>`, report row; set `"builder": "codex"` in
      `graph.json`.

## 3. The goal prompt: required sections

1. **Objective.** One paragraph; the run ends at "a reviewed local branch", never "a merged PR".
2. **Hard rules.** No Claude CLI/skills/agents. No production access. No secrets (`.env*`,
   credentials, secret-manager reads). No shared databases. No bare `git stash`. Database tasks only in
   the test environment. Base is `origin/<base>`.
3. **Read first.** Absolute paths: brief, `decisions.md` (the sections that override the plan), the
   review contract, the named lenses, the repo `AGENTS.md`/`CLAUDE.md`. List only what the segment
   needs — a read-everything list burns the run's context before it writes any code.
4. **State at handoff.** What is merged (with SHAs), what is open, and which branches and files belong
   to *other sessions*. Give `gh pr diff <n> --name-only` for each, and the rule: touching one of those
   files means STOP.
5. **Scope fences.** Exact files or directories in scope, and an explicit "do NOT" list: no migration
   unless named, no new callers outside the listed ones, no API-spec change unless named, no fixture
   re-records.
6. **Definition of done (per segment).**
   - [ ] targeted tests for every new branch, which change the run count. Mutate each new
         guard/refusal, see a test fail, restore it;
   - [ ] the repo's guard/census tests that targeted runs miss (name them);
   - [ ] a callsite search (`rg`) for every changed public method/constant, with the list written in
         the report;
   - [ ] linter on changed files plus the repo's static checks, and API-spec lint + regenerated clients
         when a spec changed;
   - [ ] `ci-preflight` green on the final SHA. An infrastructure failure (killed run, no receipt) is
         **infra-red**, not green; retry at most once;
   - [ ] `git diff --stat origin/<base>...HEAD` shows only this segment, and the total is ≤ the cap.
7. **Commits — the driver commits, not Codex.** Codex's `workspace-write` sandbox blocks `.git` (in a
   worktree, with `--add-dir` on the common git dir, and in a plain clone alike). So the night runs as
   a **shell driver** (zero Claude tokens): per step it runs a sandboxed `codex exec` build, then
   `git add -A && git commit` itself, then `ci-preflight` and a read-only Codex self-review.
   Conventional messages, header ≤100 chars. Do not run Codex with the sandbox off to get commits: it
   runs unattended on a machine holding credentials. Never rebase or force-push a branch someone has
   already reviewed.
8. **Stop conditions.** Write `STOP-<seg>.md` (what, evidence, options, recommendation), leave the
   branch committed, and move to the next *independent* segment. Do not improvise when:
   - the brief or `decisions.md` does not settle a product/security choice ("elapsed time is not
     approval");
   - the diff would exceed the cap, or needs a file outside the fences or a migration not in the brief;
   - a test fails and you cannot explain it from your diff. First reproduce it on untouched
     `origin/<base>`; if the base fails too, log it as inherited and continue;
   - your own review's round-2 findings were created by your round-1 fixes (fix-streak);
   - a base segment stopped.
9. **Never:** `git push`, `--no-verify`, `gh pr create`, `gh pr merge`, `review-receipt claude`, or
   triggering the review bot. Codex ends at a committed local branch plus a Codex receipt; the lead
   pushes.
10. **Morning report.** `epics/<epic>/CODEX-RUN-<date>.md`, rewritten (not appended) after each state
    change, ≤ 60 lines. A table (segment, branch, base, head SHA, lines, tests run, preflight state,
    self-review verdict, state: `ready-for-review` / `stopped-<reason>` / `not-started`), then ≤5 lines
    per segment: open findings, any product choice it made (flagged), the STOP file path. An
    append-only prose log grows past the point where the lead can triage it.
11. **Context hygiene.** `rg -n` plus targeted line ranges, never whole large files; long output to a
    log file with the exit code printed, cited by path; read back only failures and the summary.

**Invocation.** Run from the worktree, with stdin redirected.
```bash
cd "$WT" && codex exec -C "$WT" -s workspace-write --skip-git-repo-check - < step-prompt.md
git -C "$WT" add -A && git -C "$WT" commit -m "<type>(<scope>): <step>"     # the driver, not Codex
```

## 4. Morning handoff (the lead)

- [ ] Read the report and each `STOP-*.md`; file real decisions with `adw-ask`.
- [ ] Per `ready-for-review` segment, in dependency order: round 1 = **fresh `reviewer` agent** +
      **fresh Codex** (contract + lenses) while `ci-preflight` runs. Never only the builder's model.
      Adjudicate.
- [ ] Worker (or Codex) fixes; Codex alone reviews the delta (fix-streak stop applies). Both receipts,
      then push per `/adw` node 7.
- [ ] Review-bot loop: ≤3 rounds, reply on every thread. High-risk / needs-a-human → the user at once.
- [ ] Never `--delete-branch` a stack base: deleting it can auto-close the PRs stacked on it.
- [ ] Record the outcome on the segment in `graph.json` so Codex and worker builds can be compared:
      `{"id":"S3","builder":"codex","lines":<n>,"bot_rounds":<n>,"claude_tokens":<n>,"merged_at":"…","stop":null}`

## 5. Anti-patterns, and the rule that prevents each

| Seen | Rule |
|---|---|
| Codex pushed with `--no-verify`, opened ready PRs and drove the review bot itself, with no Claude pass; its self-review approved and the bot then found criticals. | Codex never pushes. Round 1 is a fresh reviewer + fresh Codex, before any push. |
| A deep overnight stack built on a base that had stopped high-risk; approved children could not merge. | Stack only on merged bases; a stopped base stops its dependents. |
| Multi-thousand-line slices took dozens of commits and stopped on fix streaks or criticals. | ≤ ~800 lines, split at decomposition. |
| Many drafts opened at once; preflights queued deep and were killed. | ≤3 segments a night; sequenced preflights; infra-red is not green. |
| Review rounds kept finding gate-off parity regressions. | Gated work lands a parity harness first. |
| A "kill switch" that no runtime setting or rebuild could flip. | Every switch has a test that flips it, and is plumbed through deploy config. |
| An overnight log too long and unstructured to triage. | Fixed ≤60-line report, rewritten in place. |
| One run asked to build many slices *and* drive each through the review bot for most of a day. | One night = build + self-review to a local branch. The bot loop is a daytime job. |

## 6. Template goal prompt

```markdown
# Codex goal: <epic> <segment ids> (<date> overnight)

You are Codex, working alone overnight. Build each segment below to a **self-reviewed local branch**
(the driver script commits after each of your steps — you cannot write to `.git`; don't try). You do
not push, open PRs or merge. In the morning, Claude reviews and ships.

## Hard rules
- No Claude CLI/skills/agents. No production access, deploys or production tasks.
- No secrets: never read `.env*` or credentials; never read from the secret manager.
- Never `git push`, `--no-verify`, `gh pr create|merge`, `review-receipt claude`, or trigger the review bot.
- Never touch shared databases; database tasks only in the test environment; no bare `git stash`.
- Base is `origin/<base>` (`git fetch origin <base>` first).
- Do not edit files owned by: <PR n, branch x — `gh pr diff n --name-only`>. Needing one = STOP.

## Read first
- <abs path brief>, <abs path decisions.md §…> (overrides the plan where they differ)
- $ADW_HOME/review/review-contract.md; lenses: <$ADW_HOME/review/lenses/x.md …>
- <repo>/AGENTS.md (house style)

## Segments (independent unless stated)
### <SEG-A> — <one line>   worktree: <abs path>   branch: codex/<epic>-<seg-a>   base: origin/<base>
- Objective: <behaviour after the change>
- In scope: <files/dirs>.  Out of scope: no migration, no new callers outside <list>, no fixture re-records.
- Must test: <cases>.  Guard/census tests to run: <list>.
- Cap: ≤ 800 changed lines incl. tests. Over the cap = STOP.

## Per-segment loop
1. Build in small steps; end each step with the files written and a one-line conventional commit
   message in `STEP-MSG.txt` (header ≤100 chars) — the driver commits it.
2. Tests: single-process with a private test DB. Mutate each new guard, see a failure, restore. Run the
   guard/census list. Output to a log file; read back the exit code, failures and summary.
3. `rg` every changed public method/constant; list the callsites in the report.
4. Linter (changed files) and the repo's static checks; API-spec lint + regenerated clients if a spec changed.
5. Stop after the build steps and write the report rows; the **driver** (outside your sandbox, which
   blocks `.git` and network) then runs, per segment: a read-only Codex self-review in a fresh process
   (`codex exec -C <wt> -s read-only "<contract + lenses> TASK: falsify <claims> in git diff origin/<base>...HEAD" < /dev/null`),
   feeds findings back to you for one fix round (fix-streak → STOP), then `ci-preflight` once on the
   final SHA (green / red:<job> / infra-red), one preflight at a time in segment order, then
   `review-receipt codex pass "<what was checked>"`.
   If the sandbox blocks the local test DB in step 2, the driver runs the tests too and pastes the
   summary back.

## STOP (write <epic dir>/STOP-<seg>.md: what, evidence, options, recommendation; then next segment)
Unsettled product/security choice · over the cap · a file outside scope · an unnamed migration ·
a failure you cannot explain (reproduce on untouched origin/<base> first) · fix-streak · a base segment stopped.

## Morning report: <epic dir>/CODEX-RUN-<date>.md (rewrite in place, ≤60 lines)
| seg | branch | base | head | lines | tests run | preflight | self-review | state |
State ∈ ready-for-review / stopped-<reason> / not-started. Then ≤5 lines per segment: open findings,
any product choice you made (flagged), the STOP file path. No diffs, no logs; cite log paths instead.
```
