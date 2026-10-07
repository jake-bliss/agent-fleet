# Lens: cross-tenant-isolation

**Actor:** reviewer (round 1) · **Runs:** on the diff, BEFORE the PR opens, in any multi-tenant app

A leak here means one customer's data becomes visible or actionable to another. `callsite-coverage`
already owns two related checks — its #7 (lookup on a nullable column behind a PARTIAL unique index)
and its #8 (every new query on a tenant-scoped table is scoped). This lens assumes those are covered
and goes deeper into HOW tenancy is wired. Do not re-file a #7/#8-shaped finding here.

Before applying it to a repo, write down that repo's answers to check 1 and check 2 (the tenant
column, the two-hop tables, how request-local tenant context is set). Without them the lens is
guesswork.

## What you are looking for

1. **Not every table carries the tenant column directly.** Read the schema and sort tables into three
   groups: those with the tenant column (`tenant_id`, `account_id`, …) directly; those one or more
   join hops away (they carry only a parent id, and the parent carries the tenant); and shared
   reference/infrastructure tables with no tenant at all. Any new query or join the diff adds against
   a hop-away table must scope through the parent chain, not assume a tenant column exists on it.
2. **Request-local tenant context does not exist outside a request.** Many frameworks keep "the
   current tenant" in a thread-local or request store that only middleware sets. A job, scheduled
   task, CLI command or console never passes through that middleware. If the diff moves code that
   reads the current tenant into a job, service or task, it silently gets `nil` there — a quiet empty
   result, not a crash review would catch.
3. **Authorization vs. scoping.** A record found globally (by an unguessable token) and then
   authorized by comparing tenant ids is acceptable only if the comparison gates BEFORE the record is
   read or mutated, and fails closed (403), not logged-and-continued. The safer shape scopes at query
   time (`current_tenant.things.find(id)`), where a wrong id is a not-found, not a leak. A diff that
   swaps query-time scoping for global-find-plus-manual-check is a downgrade; demand a reason.
4. **Serializer/response leakage through an association**, even when the root record was scoped. An
   attribute that traverses an association must land on a shared reference table, or on a record
   independently confirmed to belong to the SAME tenant as the root.
5. **Jobs receive a bare id and reload it — tenant identity must come from the loaded ROW.** The
   finding shape: a job loads row A (establishing tenant X), then uses a SECOND identifier from its
   payload (an external id, another row's id) to look up row B on another table WITHOUT checking B
   belongs to the same tenant as A.
6. **Cache keys must carry a tenant identifier.** A new cache key built only from entity-level
   identifiers (an external id, an enum, a filter param) lets one tenant's cached response serve
   another tenant's request whenever those identifiers collide across tenants. The same applies to
   rate-limit keys, lock keys and idempotency keys.
7. **Joins reaching a hop-away or no-tenant table must resolve the tenant boundary at the join**, with
   an explicit tenant predicate, not assume the child table filters itself.
8. **Unauthenticated paths that set tenant context.** An endpoint that skips authentication and sets
   the current tenant is safe only if the selector is itself an unguessable, tenant-bound token. One
   that accepts a plain tenant id, slug or email from the client is a direct impersonation vector —
   flag it regardless of what else the action does.

## How to run

- Read the diff for new/changed queries and joins, new serializer attributes, new job signatures, new
  cache/lock/rate-limit keys, and any new skip-authentication action.
- For each, trace which table it touches and check it against the grouping from check 1.
- For any read of request-local tenant context, confirm the call site is reachable only from a
  request.
- A finding names the file:line and the concrete cross-tenant scenario: which OTHER tenant's data
  becomes visible or actionable, and how.

## Verdict schema (return this)

```json
{
  "lens": "cross-tenant-isolation",
  "verdict": "pass | fail",
  "findings": [
    {
      "file": "src/...",
      "line": 0,
      "gap": "hop-away-table | context-outside-request | find-then-authorize-missing-check | serializer-leak | job-untrusted-cross-lookup | cache-key-missing-tenant | join-missing-tenant-predicate | unauthenticated-context-set",
      "scenario": "concrete cross-tenant read/write this enables",
      "confidence": "high | medium"
    }
  ]
}
```

`verdict: fail` if any high-confidence finding exists. Empty findings → `pass`.
