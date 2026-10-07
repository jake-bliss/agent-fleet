# CLAUDE.md snippet

Paste the parts you want into your own `~/.claude/CLAUDE.md`. Each section stands alone. They assume
`adw` is installed (`install.sh`) and that you run your agents in herdr. Replace "me" with however you
want your agents to refer to you.

---

## Decisions go through `adw-ask`

When I have to decide something you cannot default, file it with `adw-ask` as well as saying so in
chat. It lands on the ADW Board (http://127.0.0.1:4518/), where I answer; the answer arrives in your
pane as a message starting `[adw-ask <id> answered]`. Chat scrolls away; the board does not.

```bash
adw-ask new --title "<one line>" --epic <slug> --body-file /tmp/<id>.md \
  --option "a=<choice>" --option "b=<choice>" --recommend a     # prints d-xxxxxx
```

- The body must let me decide **without opening your tab**: context, evidence (file:line, PRs,
  numbers), each option's consequence, your recommendation and why. Markdown.
- After filing, mention the id in one line, then continue other work or end the turn. Never poll.
- Withdraw a question that resolves itself: `adw-ask withdraw <id>`.
- Only works inside a herdr pane (`HERDR_PANE_ID` set); elsewhere ask in chat.

## Declare long-running state with `adw-status`

So the board can tell "waiting" from "stalled":

```bash
adw-status waiting "<what, e.g. review bot on PR 123>" --for 30m   # or --until <ISO time>
adw-status done "<one line: what finished>"
adw-status failed "<one line: what broke>"
adw-status lead <epic>                                            # this tab leads an epic
```

Use it in any tab running multi-step work: `waiting` right before ending a turn to wait on CI, the
review bot, subagents or a person; `done` / `failed` when the work ends. A tab that reported once and
then stops without a new report shows as **stopped silent**; a wait past its deadline shows as
**overdue**. Plain chat tabs that never call it are left alone.

## Context discipline — the bill is context × turns

Every turn re-reads the whole context, so input dwarfs output (in one measurement, output:input was
~1:300). A file opened and not needed costs its size × every remaining turn, not once. Optimise for
what enters context, not for output length.

- **Batch independent tool calls into one message.** A call's cost is dominated by the turn it sits
  in, not its output. Call count is the variable.
- **Never re-read a file already read this session.** Edits fail loudly on stale content, so
  re-reading "to verify" buys nothing.
- **Filter before ingesting.** Narrow a search pattern rather than truncating its results.
- **Delegate wide reads** — subagent context is isolated; only the conclusion returns. This is the
  largest single lever.
- **Say so when a `/clear` is due.** Context carries across unrelated tasks until I clear it; I
  cannot see the cost, and only I can reset it.

## Comments: write none unless the repo requires it

Default to **no comment**. Write one only when it is *required*:

- **Doc comments on public methods**, where the repo's standards call for them. Describe the
  signature and what it returns — not how it works inside.
- **Load-bearing directives**: linter disables, pragmas, coverage markers, annotations a tool reads.
- **A non-obvious constraint a reader would otherwise violate** — a lock ordering, an API that
  rejects a field when blank, a deliberate swallowed error. One sentence, naming the constraint.

Do NOT write: narration of how code works, historical notes about what used to be there, rationale
essays, tombstones for deleted code, or a restatement of the lines below the comment.

**Why.** Prose about mechanism is verified by nothing and rots silently, yet it reviews as
authoritative — so a wrong comment is worse than none. Repeated attempts to describe one subtle
mechanism in a comment tend to each introduce a new factual error, often in the fix for the previous
wrong version.

**Where the knowledge goes instead**, in order of preference:
1. **A test.** It executes, so it cannot rot. "Read that test, not a comment" is the only comment of
   this kind worth writing.
2. **A dated evidence file** in the repo for measurements, production numbers and the conditions
   under which to re-measure.
3. **The commit message / PR body** for why a change was made.
4. **Nowhere.** If it is not worth a test or an evidence file, it is not worth a paragraph in a
   source file.

This applies to code you write or edit. Do not strip existing comments as a side errand — but when
editing a block whose comment is already wrong, delete the wrong part rather than correcting it,
unless the corrected claim is independently verifiable.

## Delegation

Default to delegating. The main thread is the orchestrator, not the worker.

| Role | Model | Use for |
|---|---|---|
| orchestrator | the strongest model | planning, briefs, adjudication, git, talking to me |
| `Explore` | any | fan-out search; returns the conclusion, not file dumps |
| `worker` | a cheaper model | scoped implementation inside an assigned worktree |
| `reviewer` | the strongest model | adversarial defect hunt on a diff (read-only) |
| second model (Codex) | a different vendor | reviews, plan critiques, big mechanical refactors |

**Delegate when:** answering needs reading across several files; work splits into independent parts
(launch them in ONE message so they run concurrently); a refactor is large and mechanical; an
independent review is wanted. **Don't** delegate a single-fact lookup in a known file, anything one
edit finishes, or work a skill already owns.

**Model routing:** a cheap model for mechanical extraction, a mid model for building, the strongest
for architecture, subtle bugs and orchestration. Cheapening the orchestrator is a false economy.

## Nothing reaches remote CI unverified

Remote CI and review bots are shared, rate-limited and paid for. They exist to **confirm** a result
already known locally, never to discover it. Before any branch is pushed:

```bash
ci-preflight                                       # the repo's own PR CI, run locally, in CI's order
review-receipt claude pass "<what the reviewer found>"
review-receipt codex  pass "<what Codex found>"
git push                                           # the pre-push hook verifies all three
```

The hook blocks the push unless local CI is green on the exact SHA, Codex passed the exact SHA, and
the Claude review passed this SHA or an ancestor of it. Details: `docs/ci-gate.md` in the adw repo.

`--no-verify` is acceptable only when the diff touches nothing CI checks, when the branch is a pure
revert of commits already on the default branch, or when CI is green and the reviews are recorded but
the hook still blocks (a tooling fault — fix it afterwards). Never past a red preflight or a missing
review.

**A gate whose red means "my own tooling is missing", or whose green means "the job was skipped", is
worse than no gate, because it is trusted.** Read the job list in the log, not just the exit code.

## Cross-model review is the default

Claude and Codex miss different things. For anything that will be merged, round 1 runs BOTH over the
same diff and you adjudicate — never relay one side's findings unverified.

- **Diffs, before the push** → a fresh `reviewer` agent and Codex in parallel, while `ci-preflight`
  runs. Adjudicate, record the receipt. The worker fixes; **Codex alone** reviews the fix delta plus
  the behaviour it could break. **Fix-streak stop:** if round-2 findings were created by round-1
  fixes, redesign (a new round 1 with both) or ask me — never a third patch round.
- **Plans, before any code** → critique with Codex read-only and reconcile against your own read.
- **Adjudicate, don't concatenate.** When the two sides disagree, verify the disputed line yourself
  and say which side was right.

## After the push — the review bot loop, then merge

If the repo has a PR review bot whose approval counts toward branch protection, work it for **at
most 3 rounds**: triage its inline comments as review findings, reply on every thread with the fix
commit or why not, and send fixes through the worker, a Codex delta review and `ci-preflight` before
the next push. If it flags the PR high-risk or needs-a-human, ask me at once rather than spending
rounds it cannot approve. Not approved after 3 rounds → ask me with what is open.

**Merge authority:** the bot approved the current head + CI green + merge state `CLEAN` → squash-merge
without asking, then watch the deploy. Everything else is my call. In repos without a bot, I approve.

## Codex invocation — the two traps

- Always redirect stdin: `codex exec ... < /dev/null`, or it blocks forever with no output.
- Always pass `-C <absolute worktree path>` so it runs in the right root.

## Worktrees

All build work happens in a fresh git worktree off the remote default branch, never in a main
checkout. See `docs/worktrees.md` in the adw repo for `cow-worktree`.
