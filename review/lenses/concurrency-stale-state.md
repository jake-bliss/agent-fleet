# Lens: concurrency-stale-state

**Actor:** Claude and Codex review nodes · **Runs:** on the diff, BEFORE the PR opens
**Auto-selected for:** code under job, worker or service directories, or paths containing `lock`, `ledger`, `sync`.

## What you are looking for

The question for every read-then-write in the diff: **what can change between the check and the write?**
Tests run one actor on a quiet database; production runs retries, parallel workers, and replicas.

1. **Check-then-act without a lock** - `find_by`/`exists?`/`count` followed by `create`/`update`/an external call.
   Is there a row lock (`with_lock`, `lock!`), advisory lock, unique index, or `upsert` that makes it atomic? A
   rescue of `RecordNotUnique` that re-reads is fine; one that proceeds on stale data is not.
2. **Retries after state changed** - a job or HTTP retry that re-runs a non-idempotent step (charge, email,
   external create) after the first attempt partly succeeded. What is the idempotency key, and is it recorded
   in the same transaction as the effect?
3. **Locks that don't cover the write** - lock taken on the wrong row or after the read; lock released (end of
   transaction, `ensure`) before the dependent write; lock ordering that can deadlock with another path.
4. **Stale caches and memoized reads** - an in-process memo, a shared cache, preloaded associations, or arguments
   serialized into a job at enqueue time and used minutes later. Re-read the authoritative row at write time?
5. **Replica / NOLOCK / snapshot reads** - reads from a replica, `WITH (NOLOCK)`, or a long transaction
   feeding a decision that is then written to the primary.
6. **Authz and tenant scope on cached data** - a permission or tenant id decided at enqueue or cache fill
   and trusted later, after the user, role, or ownership changed; cache keys missing the tenant.
7. **Status transitions** - `update(status: x)` that does not condition on the current status, so a late
   worker overwrites a newer terminal state.

## Output

Per finding: file:line, the two interleaved actors/timelines that break it, the observable wrong outcome,
and the one-line fix (lock, conditional update `where(status: ...)`, unique index, re-read). If the code is
single-writer by construction, say what guarantees that rather than passing silently.
