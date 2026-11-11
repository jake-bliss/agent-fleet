# Lens: flag coverage

The question: with `<FLAG_KEY>` off, does production behave exactly as it does on the base branch?

For every added or changed line, find the flag check that guards it, or show it is inert without one.
Check each of these:

- **Entry points.** Routes, controller actions, GraphQL fields, API params, jobs and their schedules,
  callbacks (`before_*`/`after_*`), event subscribers, webhooks, rake tasks. Each new one is gated,
  or unreachable when the flag is off.
- **Changed existing paths.** An edited method that runs today must keep its old behaviour on the off
  branch. Look for refactors that changed the shared path while only the new branch reads the flag.
- **Data.** Migrations, defaults, backfills, validations and serializer output cannot sit behind a
  flag. Each must be additive and invisible to flag-off code (new nullable column, new table, no
  changed default, no tightened validation, no new key in an existing payload).
- **The flag read itself.** Correct key, the right actor or company, off as the fallback when the
  flag service errors, and not cached across tenants.
- **Frontend.** New UI, requests and side effects render or fire only when the flag is on.

A path that runs with the flag off and changes behaviour is IMPORTANT at high confidence. Name the
entry point and the flag-off request that reaches it.
