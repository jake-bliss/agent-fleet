# Lens: adversarial-review (clean-context agent)

**Actor:** a SEPARATE agent, dispatched by the orchestrator · **Runs:** `/adw` round 1, before the PR opens

## Why this node exists

The orchestrator writes the brief, then reviews against the brief. **Same mental model twice.** The
defects that escape that kind of review sit outside what the brief anticipated — that is structural,
not bad luck. Typical escapes:

| Caught later by | Defect |
|---|---|
| a human reviewer | a lookup on a nullable column behind a PARTIAL unique index returned a row from **another tenant**, on a payments path |
| a human reviewer | a duplicate method definition invisible to the linter inside a DSL block |
| a review bot | an adapter returned a stale in-memory object after delegating |
| a review bot | events published a stale payload after a swallowed failure |
| remote CI | a timestamp precision comparison passed locally and failed on CI |

Green tests are **not** evidence here: the same model run wrote the code and the tests, to the same
brief. Green means "the thing I specified happens," not "nothing else broke."

## The contract

Dispatch a fresh agent that has **NOT seen the brief**. Give it the diff, the repo's `CLAUDE.md`, the
review contract and the sibling lenses. Its job is to *find defects*, not to confirm compliance.
Brief compliance is a SEPARATE, later check by the orchestrator — never fold them together, or the
agent inherits the blind spot.

## Prompt shape (adapt per segment)

> You are reviewing a diff you did not write, for a codebase you should read before judging. Nobody has
> told you what this change was *supposed* to do — infer intent from the code, then attack it.
>
> Read the repo's `CLAUDE.md` for house style. Read the diff with `git diff <merge-base>`.
> **Diff against the merge-base, never a moving `origin/<default>`** — the default branch advances, and
> every file added since the branch point renders as a spurious deletion.
>
> Report ONLY defects you can state as a concrete failure: file:line, the input/state that triggers it,
> and what an end user or subscriber observes. No style opinions unless they violate `CLAUDE.md`.
> If you find nothing real, say so plainly — a clean report is a valid result and far better than padding.
>
> **Enumerate the failure paths explicitly.** Most escapes are here, not on the happy path:
> - What does the caller receive when the operation half-succeeds?
> - What does a webhook/event subscriber observe on failure? Stale payload? Nothing? Twice?
> - What is returned/rendered when a nullable value is nil?
> - Does an object mutated in memory match what was persisted?
> - Does a post-commit/deferred step change what a synchronous caller sees?
>
> **Query-level traps to check every time:**
> - A lookup on a **nullable** column whose unique index is **partial** — find the index definition
>   and read its `WHERE`. `... IS NOT NULL` permits unlimited NULLs, so a lookup by NULL is an
>   unordered scan returning an arbitrary row. On a tenant-scoped table that is a cross-tenant read.
> - Any new query on a tenant-scoped table that is not tenant-scoped.
> - A lookup chain **re-implemented** instead of delegating to the existing one — diff it against
>   the original **guard for guard**; a dropped blank-check is invisible in tests.
> - Reads that WRITE: a `fetch`/`find`/`resolve` that inserts or updates.
> - Rescues attached to a whole method body, mapping any nested error to one specific domain meaning.
>
> **Tool limits you may NOT cite as evidence of safety:**
> - Linters miss whole classes of defect inside DSL blocks and metaprogramming.
> - Green CI cannot see a bypass route no test exercises.
> - Tests written alongside the code prove intent, not absence of regression.
>
> Also run the sibling lenses as written, not from memory — at least `callsite-coverage.md`.

## Orchestrator's job after the agent reports

1. **Verify each finding yourself** against the code before acting — the agent can be wrong, and
   relaying an unverified finding is the same failure in a new coat.
2. Only then check brief compliance (the separate pass).
3. Only a contract `fail` goes back to the build node: any CRITICAL, or an IMPORTANT at **high**
   confidence (`../review-contract.md`). Medium/low confidence, suggestions, "latent", "worth a
   decision" and PRE-EXISTING findings go under **Known / deferred** in the PR body — they do not
   re-enter the loop.
4. After a fix, re-run tests, then **Codex alone** re-reviews the fix delta
   (`git diff <reviewed SHA>..HEAD`) plus the existing behaviour the fix could break — no second
   reviewer-agent pass. **At most 2 rounds total** — a blocker still standing after round 2 goes to
   the user, not to round 3. If round 2's findings were created by round 1's fixes, stop patching:
   redesign (new round 1, both reviewers) or surface to the user. Past round 2, full-diff re-reviews
   tend to produce only medium/low-confidence findings on surface the previous fix created.
5. Record any NEW trap class in this file and in `callsite-coverage.md`, so the next run inherits it.
