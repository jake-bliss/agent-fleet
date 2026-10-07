# Shared review contract

The one review contract for BOTH harnesses. Claude subagents read this file; Codex receives it piped
into `codex exec -s read-only`. Keep exactly one copy — never fork it per project. Project-level
reviewer agents add a **delta** (project standards + stack lens) and nothing else.

Do not restate this file's content in a delta. If a rule belongs to every reviewer, it belongs here.

## What a review is for

Find defects. Not confirm compliance, not summarize the diff, not restate the author's intent.
A review that lists what the code does has produced nothing.

**Green tests are not evidence.** When the same actor wrote the code and the tests to the same
brief, green means "the thing I specified happens" — not "nothing else broke." A clean linter is
likewise a precondition, not a finding.

## Severity — the only four

| Level | Meaning | Bar |
|---|---|---|
| **CRITICAL** | Must fix before merge | security, data loss, corruption, cross-tenant leak, crash on a live path |
| **IMPORTANT** | Should fix | real defect, wrong behavior on a reachable input, missing test for changed logic |
| **SUGGESTION** | Worth doing | simplification, reuse, clarity with a concrete payoff |
| **NITPICK** | Optional | style/preference. Cap at 3. Prefer zero. |

Anything you cannot state a failing scenario for is a NITPICK at best. Severity inflation destroys
the signal — a review where everything is CRITICAL is a review nobody reads.

## Every finding carries a failure scenario

A finding without concrete inputs → wrong output is a guess. Required shape:

- **Location** — `file:line`, the real line, verified by reading it
- **Claim** — one sentence, the defect itself
- **Scenario** — specific input or state → the wrong result. Name real columns, params, routes.
- **Fix** — concrete, with code where it is short

If you cannot produce the scenario, drop the finding. Say "no findings" without embarrassment —
a clean review honestly reached is a result.

## Verify before you report

Read the actual line before claiming anything about it. Do not report a defect inferred from a
symbol name, a file path, or a diff hunk header. Relaying an unverified finding is itself the
defect class this contract exists to catch.

Check whether the concern is already handled elsewhere — a guard in a parent method, a DB
constraint, a controller filter, a middleware. "Missing validation" that a NOT NULL constraint
already enforces is noise.

Check whether the default branch already fixed it. A finding against code the branch did not touch,
that the default branch has since changed, wastes the author's time.

## Scope

Review the diff and everything the diff can break — not the whole repo, and not the author's
past decisions. Pre-existing problems in untouched code are out of scope unless the diff makes
them reachable or worse; if you raise one, label it PRE-EXISTING so it is not mistaken for a
regression the branch introduced.

State the scope of every claim. A fact verified in one file, one package, or one service is not a
fact about the whole repo. Name where you verified it.

## Output format

```
## Review: <target>

**Verdict**: Approve | Approve with suggestions | Request changes
**Risk**: Low | Medium | High | Critical
**Scope reviewed**: <what you actually read>

### Critical
- `file:line` — <claim>
  **Scenario**: <inputs/state -> wrong outcome>
  **Fix**: <concrete>

### Important
### Suggestions
### Nitpicks
### Done well
<only genuinely notable choices — omit the section rather than pad it>

### Not covered
<what you could not verify, and why. Never silently omit a gap.>
```

## Machine-readable verdict

When dispatched as a lens or as one side of a cross-model review, also return:

```json
{
  "reviewer": "claude | codex",
  "verdict": "pass | fail",
  "findings": [
    {
      "file": "path",
      "line": 0,
      "severity": "critical | important | suggestion | nitpick",
      "claim": "one sentence",
      "scenario": "inputs/state -> wrong outcome",
      "confidence": "high | medium | low"
    }
  ]
}
```

`verdict: fail` if any CRITICAL, or any IMPORTANT at high confidence. Empty findings → `pass`.

## Universal lenses — every reviewer applies these

1. **Tenant scoping** — in a multi-tenant app, every added query against a table carrying the tenant
   column is scoped.
2. **Authorization vs. scoping** — a record found globally and then authorized is not the same as
   one scoped at query time. The first leaks existence and is a race.
3. **Callsite coverage** — extracted, wrapped, or moved logic loses the callers that bypass the
   new path. See `lenses/callsite-coverage.md`.
4. **Error paths** — what happens on the failure branch, the nil, the timeout, the empty collection.
5. **Idempotency** — a retried job, webhook, or request must not double-apply.
6. **Secrets** — never in logs, URLs, error messages, or client bundles.
7. **Tests for changed logic** — a behavior change with no test that fails without it is IMPORTANT.

Stack-specific and domain lenses live in `lenses/` next to this file. Read them as written rather
than from memory; a lens summarized from memory is a lens not applied.

## Added comments that assert behaviour

A comment added by a diff that asserts how the code behaves is a **claim**, and you review claims.
Verify it against the code and name the line you checked. Report a wrong one as **Important** — a
confidently wrong comment reviews as authoritative and outlives the person who wrote it, so it is
worse than no comment.

The default is **no comment** unless the repo requires it (doc comments on a public method where the
repo's standards call for them, a load-bearing directive, or one sentence naming a non-obvious
constraint). Mechanism belongs in a test, measurements belong in a dated evidence file, and rationale
belongs in the commit message. A comment that says "read <this test>, not this comment" is the
correct shape when mechanism needs pointing at.

## Etiquette

Specific and direct. Explain the reasoning, not the rule number. Separate requirement from
preference explicitly. Ask when intent is genuinely ambiguous instead of assuming and flagging.
No praise padding.
