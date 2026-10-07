# Lens: migration-lock-safety (Rails/Postgres example)

**Actor:** reviewer (round 1) · **Runs:** on any diff that touches schema migrations

This is an EXAMPLE lens for a Rails app on Postgres with no automated migration linter (such as
`strong_migrations`) and no global lock timeout. Adapt the idioms to your stack; the hazards are
Postgres's, not Rails's.

The failure it guards against: a DDL statement waits for a lock behind one long-running reader, every
new query on that table queues behind the DDL in Postgres's FIFO lock queue, and the app is down until
someone terminates the blocker by hand. CI never shows it — its near-empty database has no concurrent
readers to queue behind.

Before applying it, list the repo's **hot tables**: anything a request path, webhook, poller or job
reads or writes continuously. Confidence on a finding depends on whether the table is hot.

## What you are looking for

1. **ACCESS EXCLUSIVE operations.** Postgres takes ACCESS EXCLUSIVE (blocks ALL reads and writes for
   the statement's duration, and queues behind any reader) for: adding a column with a volatile
   default; adding `NOT NULL` (full scan unless a validated CHECK already proves it); adding a foreign
   key without `validate: false` (scans both tables); `add_index` WITHOUT `algorithm: :concurrently`;
   column type changes (table rewrite); renames (instant, but still queue behind a slow reader);
   `drop_table`. On a hot table, demand the lock-timeout guard from check 3.
2. **Concurrent index builds.** `algorithm: :concurrently` requires `disable_ddl_transaction!` — missing
   it is a hard finding. A failed concurrent build leaves an INVALID index (`pg_index.indisvalid =
   false`) behind, and a retry with `if_not_exists: true` sees the name taken and skips the rebuild —
   silently leaving a full scan (or an unenforced unique constraint) while the migration reports
   success. A concurrent build on a hot table with no recovery path (check `indisvalid`, then
   `DROP INDEX CONCURRENTLY` and rebuild) is a soft finding.
3. **A lock timeout on every migration that can queue on a hot table.** Inside an ordinary
   transactional migration: `execute "SET LOCAL lock_timeout = '10s'"`. With
   `disable_ddl_transaction!`: plain `execute "SET lock_timeout = '5s'"` — `SET LOCAL` needs an open
   transaction and silently does nothing without one. Neither idiom present on a hot table is a
   finding: the migration will queue indefinitely instead of failing fast and letting the deploy
   retry.
4. **Backfills are batched and narrowed — never a bare `update_all` over an unbounded table.** Use
   `find_each` (or `in_batches`) over a narrowing `where`, with a per-row skip guard so only rows that
   need the change are touched. `update_all` runs as one statement holding row locks for its full
   duration and cannot resume after a timeout.
5. **Destructive changes need a multi-step sequence.** Before a column or table is dropped, every
   model/query reference to it must already be gone (via `self.ignored_columns` in an earlier deploy,
   or by removing the references in earlier commits). A drop underneath a live read is the outage
   shape above.
6. **Stacked DDL with no guard.** Enum `ADD VALUE` mixed with other DDL in one migration, or several
   unrelated DDL kinds in one un-timed migration, is its own finding regardless of table size today —
   a migration will one day run against a restore of production-sized data.
7. **"Linter clean" and "migration ran in CI" are not evidence of lock safety.**

## How to run

- List every migration file the diff touches.
- Classify each DDL statement against check 1, then apply checks 2–6 where relevant.
- Decide whether the target table is hot — that sets high vs medium confidence.
- A finding names the migration file, the unsafe statement, and the concrete lock/duration
  consequence on that table.

## Verdict schema (return this)

```json
{
  "lens": "migration-lock-safety",
  "verdict": "pass | fail",
  "findings": [
    {
      "file": "db/migrate/...",
      "line": 0,
      "hazard": "access-exclusive-no-timeout | concurrent-index-no-disable-ddl | invalid-index-recovery-missing | unbounded-backfill | destructive-drop-no-multideploy | stacked-ddl-no-guard | other",
      "scenario": "what queues/breaks, for how long, against which table",
      "confidence": "high | medium"
    }
  ]
}
```

`verdict: fail` if any high-confidence finding targets a hot table. Empty findings → `pass`.
