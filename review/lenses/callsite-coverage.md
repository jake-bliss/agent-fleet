# Lens: callsite-coverage

**Actor:** reviewer (round 1) · **Runs:** on the diff, BEFORE the PR opens

The most common defect class in agent-written changes. Green CI and a review bot's top score do NOT
catch it.

## What you are looking for

When the diff **extracts** logic (callback → service), **wraps** a return value, or **moves** a
behavior behind an abstraction, it frequently misses call sites that bypass the new path. Find them.

Check every one of these bypass routes against the changed behavior (examples use ORM terms; map them
to your stack):

1. **Cascading deletes** — does the new logic run when a parent is destroyed and children cascade
   (ORM `dependent:` options, DB `ON DELETE CASCADE`)? Hooks on the child may be skipped.
2. **Bulk operations that skip hooks/validations** — `delete_all`, `update_all`, `insert_all`,
   `upsert_all`, bulk imports, raw SQL. If the moved logic lived in a hook, these bypass it.
3. **Direct construction** — call sites that create or update the record directly (`create!`,
   `new.save`, column-level updates) instead of going through the new service/method.
4. **Externally rendered return values** — serializers, templates, API responses, or jobs that read
   the OLD shape of the return the diff just wrapped.
5. **Other callers of the extracted method** — grep the symbol across the repo; every caller must
   still get correct behavior, not just the one the PR touched.
6. **Re-implemented resolution chains** — when the diff inlines a lookup that previously went through
   a shared helper, diff it against the original **guard for guard**. A dropped
   `return nil if x.blank?` is invisible in tests and catastrophic in production — a copied chain
   that loses its blank guard can bind records across tenants. The fix is always *delegate to the one
   implementation*, never re-add the guard to the copy.
7. **Lookup on a nullable column with a PARTIAL unique index** — find the index and read its `WHERE`.
   `WHERE (... AND col IS NOT NULL)` permits unlimited NULLs, so a lookup by NULL is an **unordered
   scan returning an arbitrary row**, not a lookup. On a tenant-scoped table that is a cross-tenant
   read. Demand a blank guard *and* a tenant scope.
8. **Tenant scoping on every new query** — any new lookup against a tenant-scoped table must be
   scoped to the tenant unless there is a stated reason not to be.

## Tool limits you may not cite as proof

- Linters miss duplicate definitions and similar defects inside DSL blocks and metaprogramming.
  "Linter clean" is not evidence of their absence.
- Green CI cannot see a bypass route no test exercises. That is the entire reason this lens exists.

## How to run

- Read the diff. For each extracted/wrapped/moved symbol, grep the whole repo for callers.
- For each caller, decide: does it go through the new path, or bypass it? Bypass = finding.
- A finding names the **file:line of the bypassing call site** and the concrete broken scenario.

## Verdict schema (return this)

```json
{
  "lens": "callsite-coverage",
  "verdict": "pass | fail",
  "findings": [
    {
      "file": "src/...",
      "line": 0,
      "bypass_route": "cascade | bulk-op | direct-create | external-render | other-caller | reimplemented-chain | partial-index-lookup | tenant-scope",
      "scenario": "concrete input/state -> wrong outcome",
      "confidence": "high | medium"
    }
  ]
}
```

`verdict: fail` if any high-confidence finding exists. Empty findings → `pass`.

## Trap: two associations over the same table are two load states

A model can declare two associations over the same table (say `order_items` and `items`). They are
separate associations with independent loaded state and **separate object instances**. Preloading one
does nothing for code that reads the other — the same rows get loaded twice, and a predicate that
reads the second sees an unloaded target even though "the items are preloaded."

When a diff adds a preload or an association read, grep the model for OTHER associations over the
same class/table before concluding the preload covers the caller. Name the association the code
actually reads, not the one that sounds right.

## Trap: a query-count assertion placed after a render proves nothing

An "assert no queries" around a predicate, placed AFTER a render that already called that predicate,
passes even with the preload deliberately removed — the render lazily loaded and cached everything.

**Every query-count assertion must run against a COLD association state**, before anything else in
the test touches that object. Mutation-check every such assertion by deleting the preload it guards —
if the test still passes, the assertion is decorative.
