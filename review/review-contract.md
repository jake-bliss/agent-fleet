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
| **NITPICK** | Not reported | style, naming, preference: leave them out entirely |

Anything you cannot state a failing scenario for is not a finding. Severity inflation destroys
the signal — a review where everything is CRITICAL is a review nobody reads.

**Out of scope, never reported:** style, naming, structure and taste; YAGNI and "you might want";
risks that only matter after some future change; coverage nags that name no behaviour a test would
catch. Token spent on these is token not spent finding defects.

## Every finding carries a failure scenario

A finding without concrete inputs → wrong output is a guess. Required shape:

- **Location** — `file:line`, the real line, verified by reading it
- **Claim** — one sentence, the defect itself
- **Scenario** — specific input or state → the wrong result. Name real columns, params, routes.
- **Proof** — CRITICAL and IMPORTANT only, see **Prove it**
- **Fix** — concrete, with code where it is short

If you cannot produce the scenario, drop the finding. Say "no findings" without embarrassment —
a clean review honestly reached is a result.

## Prove it

A CRITICAL or IMPORTANT finding is proven by running something against the real code, not by
reasoning about it: a failing test, a one-off script (`python3 -c`, `ruby -e`, `node -e`), a
`git`/`rg` query that shows the missing caller, a read-only probe. Run it in place without writing
to the tree, and put the command and the decisive lines of its output in **Proof**.

When it truly cannot run where you are (needs a database, prod, the network, a live service),
write `not runnable: <why>` and walk the path line by line instead; such a finding is at most
`confidence: medium`. Never present an unrun claim as run. The orchestrator re-runs every proof
command before acting on a finding and drops the finding if the output does not show the defect.

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
  **Proof**: <command run -> decisive output, or "not runnable: <why>">
  **Fix**: <concrete>

### Important
### Suggestions
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
      "severity": "critical | important | suggestion",
      "claim": "one sentence",
      "scenario": "inputs/state -> wrong outcome",
      "proof": "command -> decisive output, or not runnable: why (critical/important)",
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
8. **Flag coverage** — a PR whose body names a feature flag merges without the owner (`pr-ship --flagged`), so
   check that every new code path, job, callback, serializer field and migration effect is off while the flag is off.
   A path that runs with the flag off is CRITICAL; a migration that is not additive and backward-compatible is CRITICAL.

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
