# Lenses

A lens is a short, focused checklist a reviewer applies to a diff on top of `../review-contract.md`.
The contract says how to review (severity, failure scenarios, verify before reporting); a lens says
what to look for in one domain. Both Claude reviewer agents and Codex read lenses verbatim — a lens
summarized from memory is a lens not applied.

What ships here:

| Lens | Applies to |
|---|---|
| `adversarial-review.md` | every round-1 review: the clean-context reviewer that never saw the brief |
| `callsite-coverage.md` | every diff that extracts, wraps or moves behaviour |
| `cross-tenant-isolation.md` | multi-tenant apps |
| `migration-lock-safety.md` | schema migrations — a Rails/Postgres example to adapt |

## Writing your own

Write a lens when the same defect class escapes review twice. Not before: a lens nobody needs is
context every reviewer pays for on every run.

Shape (copy an existing lens):

1. **Header** — the lens name, who runs it, and which diffs it applies to. `/adw` sets a high risk
   tier when a diff touches a surface a lens covers, so be precise about the trigger.
2. **What you are looking for** — numbered checks. Each one names a concrete defect shape, the
   evidence a reviewer must read to confirm it (the schema, the index definition, the callers), and
   why the usual signals (green tests, a clean linter) don't catch it.
3. **How to run** — the mechanical steps: what to grep, what to read, what a finding must name.
4. **Verdict schema** — the JSON the reviewer returns, with a `fail` rule. Keep `fail` to
   high-confidence findings so the exit rule in `/adw` stays meaningful.
5. **Traps** (optional) — specific, generalised lessons from defects that escaped. Keep the lesson,
   drop the incident: no ticket numbers, dates or names, which rot and mean nothing to the next reader.

Rules that keep lenses useful:

- **Repo facts belong in the repo.** A lens is portable; the list of your hot tables or tenant
  hop-away tables belongs in that repo's `CLAUDE.md` or a project reviewer agent's delta, where the
  lens tells the reviewer to look.
- **Don't duplicate the contract** or another lens. If two lenses check the same thing, one owns it
  and the other points at it.
- **Every check must be falsifiable.** If a reviewer cannot state a failing scenario from it, it is
  advice, not a check — cut it.
- **Record new trap classes** in the lens after a defect escapes, so the next run inherits it.

Point `/adw` at your lens by putting it in this directory (installed at `$ADW_HOME/review/lenses/`);
the skill tells reviewers to apply every lens the diff touches.
