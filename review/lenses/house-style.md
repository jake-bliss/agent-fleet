# Lens: house-style

**Actor:** reviewer (round 1) · **Runs:** parallel, on the diff, BEFORE the PR opens
**Grades against:** the repo's own skills and standards plus
`<worktree>/.claude/CLAUDE.md`. The rules below are examples of the shape; the repo's own standards
win wherever they differ.

## The rule that matters most: location signals the pattern

Many codebases split "legacy" from "target" code by directory. New code that follows the target
pattern (result objects, context passed to the constructor, domain verbs for method names) belongs in
the target directory, not the legacy one, and its tests mirror that path. Putting target code in a
legacy directory passes CI and the linter; only this lens catches it. Read the repo's guide for which
directory is which, and name the one you checked.

## Checklist (fail on any hard violation)

- **Location**: new code lives where the repo's guide says target code lives; tests mirror it.
- **Class names**: an action suffix that says what the class does. Never a generic `*Service`, never a
  bare noun. "Cannot pick a suffix" means the class does too much.
- **Result objects**: public methods return an explicit success/failure value, failures carry the
  shared failure type, and inner results are re-wrapped rather than leaked.
- **Method names**: domain verbs. The repo's forbidden generic names (`call`, `perform`, `execute`,
  `process`, `handle`) are a hard violation.
- **Arguments**: accept loaded objects rather than IDs when the repo does; programmer error raises,
  user or world error returns a failure.
- **Models**: thin; no business logic in callbacks; constrained enums; optimistic locking on mutable
  tables if the repo uses it.
- **Doc comments**: on every class and public method, in the repo's format, where the repo requires it.
- **Tests**: the repo's test style (no second framework), arrange-act-assert inline, the repo's fixture
  approach, no shared mutable setup the repo bans.
- **Mechanics**: one `attr_reader`-style declaration per line, trailing commas in multiline literals,
  and any other lint-adjacent convention the guide states that the linter cannot see.

## Frontend checklist (when the diff touches a JS or TS frontend)

- No redundant or derived state; effects only for true side effects; explicit prop and state types;
  context consumed directly rather than prop-drilled; the repo's data-fetching and validation libraries;
  the repo's file-naming convention.

## Comments: none unless required (hard check)

Flag any ADDED comment that is not one of: a doc comment on a public method where the repo requires
it; a load-bearing directive (a linter disable, a magic-comment, a tool annotation); or one sentence
naming a non-obvious constraint a reader would otherwise violate.

Flag as **Important**, not a nitpick, any added comment that **asserts runtime behaviour** ("X routes
through Y", "an unparseable value becomes nil", "these predicates are mutually exclusive", "this
branch is unreachable"). Verify the claim against the code before accepting it and say which line you
checked. A run of wrong behaviour comments, each introduced by the fix for the previous wrong one, is
the failure this check exists for. If the claim is true it still belongs in a test; if you cannot
verify it in the diff, that alone is the finding.

Accept, and treat as good practice, a comment that points at an executable reproduction instead of
describing the mechanism.

## Verdict schema (return this)

```json
{
  "lens": "house-style",
  "verdict": "pass | fail",
  "findings": [
    { "file": "src/...", "line": 0, "rule": "location|class-name|result|method-name|args|model|doc|test|other",
      "violation": "what is wrong", "fix": "the house-correct form", "severity": "hard | soft" }
  ]
}
```

`verdict: fail` if any `hard` finding exists. `soft` = consistency nits (do not block, list them).
