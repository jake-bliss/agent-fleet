#!/usr/bin/env python3
"""UserPromptSubmit hook: force a deterministic Claude+Codex cross-review.

Nothing in the harness routes work to a non-Anthropic model, and agent descriptions and rules files are
model judgment, so they fire inconsistently. A hook is harness-executed.

Scope is deliberately narrow: review and plan-critique intents only. Forcing a multi-minute
`codex exec` onto a quick edit is a cost with no payoff.

Never breaks a prompt: every failure path exits 0 with no output.
"""
import json, os, re, shutil, sys


def bail():
    sys.exit(0)


def main():
    if not shutil.which("codex"):
        bail()

    try:
        data = json.load(sys.stdin)
    except Exception:
        bail()

    prompt = (data.get("prompt") or "").strip()
    if not prompt or len(prompt) > 8000:
        bail()

    low = prompt.lower()
    adw_home = os.environ.get("ADW_HOME") or "~/.claude/adw"

    if re.search(r"/(adw|adw-lead)\b", low):
        bail()

    target = r"(pr\b|pull\s+request|diff|branch|changes?|patch|commit)"
    verb = r"(review|critique|audit|poke\s+holes\s+in|second\s+opinion\s+on|sanity[-\s]?check)"
    REVIEW = re.compile(
        rf"{verb}\s+(?:\w+\s+)?(?:this|the|my|these|that)?\s*{target}"
        r"|\bcode\s+review\b"
        r"|^/code-review\b"
        rf"|{verb}\s+pr\s*#?\d+"
        rf"|{verb}\s+#?\d+\s*$",
        re.X,
    )
    pverb = r"(review|critique|check|poke\s+holes\s+in|stress[-\s]?test|second\s+opinion\s+on)"
    ptarget = r"(plan|brief|design|approach|proposal|rfc|spec)"
    PLAN = re.compile(
        rf"{pverb}\s+(?:\w+\s+)?(?:this|the|my|that)?\s*{ptarget}"
        rf"|\b(does|is)\s+(this|my)\s+{ptarget}\b",
        re.X,
    )

    is_review = bool(REVIEW.search(low))
    is_plan = bool(PLAN.search(low))
    if not (is_review or is_plan):
        bail()

    kind = "plan" if (is_plan and not is_review) else "review"

    common = f"""## Cross-model pass required (injected by the cross-model-review hook)

This prompt matched a {kind} intent. Anthropic and OpenAI models miss different defects, so a
single-model pass here is not sufficient. Run BOTH sides and adjudicate.

**Read as written, never from memory:**
- `{adw_home}/review/review-contract.md`, the shared contract governing both sides
- every applicable lens in `{adw_home}/review/lenses/` (`callsite-coverage`, `house-style`,
  `cross-tenant-isolation`, `migration-lock-safety`)
"""

    review_body = r"""
**Run both:**
1. Claude side: dispatch the `reviewer` agent (or a stack-specific reviewer) on the diff.
2. Codex side: run the review with the contract and lenses attached:

   ```bash
   codex-review diff "<absolute worktree path>" [--base origin/<base>] [--since <sha>]
   ```
"""

    plan_body = r"""
Critique the plan with Codex BEFORE any code is written: a flawed plan caught here costs one
prompt; caught after the build it costs the whole segment.

```bash
codex-review plan "<absolute repo path>" <plan-file> --focus "what it gets wrong, omits, or assumes the code doesn't support"
```

`/adw-lead` does exactly this at its decomposition gate; match that shape.
"""

    tail = """
**Two Codex traps:**
- Always redirect stdin (`< /dev/null`) or `codex exec` blocks forever with no output.
- Always pass `-C <absolute path>`, and never route Codex through a plugin wrapper.

**Adjudicate, don't concatenate.** Where the two sides disagree, read the disputed line yourself and
say which side was right. Two lists stapled together is not a review. Report which lenses you applied
and what you could not verify.
"""

    msg = (common + (plan_body if kind == "plan" else review_body) + tail).strip()
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": msg,
        }
    }))


try:
    main()
except Exception:
    bail()
