# Retro examples and provenance

Adapted from an open-source `retro` skill, with two additions marked [LOCAL] that exist because these gaps cost real time (steps 1 and the "Unverified assertion" category).

Unverified assertion has been the most expensive failure mode twice: mechanism asserted from reading part of the code (the routing but not the index; the happy path but not the raise).

## A good finding
> **`ci-preflight` cannot resolve node and fails indistinguishably from a code failure.** Trigger: automated checks reporting the wrong thing. Verified: `ci-preflight` contains zero mise references; on a frontend diff it prints three 0-second `env: node: No such file or directory` failures and `3 job(s) failed`. Fix: resolve node via mise in the script, or exit with a distinct tooling status instead of `FAIL`. Documenting the workaround is the weak remedy — it requires the next agent to have read it.

## A bad finding
> I should be more careful about verifying claims before asserting them.

No file, no mechanism, no effect on the next session.
