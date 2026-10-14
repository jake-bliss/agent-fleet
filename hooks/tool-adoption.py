#!/usr/bin/env python3
import json, re, sys

try:
    inp = json.load(sys.stdin).get("tool_input", {})
except ValueError:
    sys.exit(0)
cmd = inp.get("command", "")
background = bool(inp.get("run_in_background"))
if "# raw-ok" in cmd:
    sys.exit(0)

RULES = [
    (r"\bcodex\s+exec\b(?=.*(read-only|\breview\b|critique))",
     "Use `codex-review diff <worktree> [--base origin/<base>] [--since <sha>]`, `codex-review plan <repo> <file>` "
     "or `codex-review ask <dir> \"<q>\"` instead of a hand-built read-only `codex exec`."),
    (r"\bgh\s+pr\s+checks\b",
     "Use `pr-state <pr>` (snapshot) or `pr-state <pr> --wait ci|ci-green` (run it in the background) "
     "instead of `gh pr checks`."),
    (r"\bgh\s+pr\s+view\b.*--json\s+\S*(statusCheckRollup|reviewDecision|mergeStateStatus|mergeable|mergeCommit)",
     "Use `pr-state <pr> [--json]` for CI/review/merge state and merge commit instead of `gh pr view --json ...`."),
    (r"\bgh\s+run\s+(watch|list|view)\b",
     "Use `pr-state --run <id> [--failures]` for one Actions run (jobs, failed-job log lines), `pr-state --runs "
     "[--branch B] [--event E] [--sha <full sha>] [--limit N]` to list runs, `pr-state <pr> --failures` for a PR head, "
     "and `pr-state <pr> --wait deployed` (backgrounded) to watch post-merge deploys."),
    (r"\bgh\s+api\b(?!.*(replies|\bmutation\b|resolveReviewThread|addPullRequestReviewThreadReply|\s-X\s|--method|--input))"
     r".*(pulls/\d+/comments|reviewThreads)",
     "Use `pr-state <pr> --findings [all]` for reviewer-bot inline findings (path:line, body, url, replied)."),
    (r"\bgh\s+pr\s+(ready\b(?!.*--undo)|merge\b)",
     "Use `pr-ship <pr> [-R repo] [--dry-run]`: it marks a green draft ready or squash-merges only when the merge rules "
     "hold (ours, default base, CI green, bot-approved on head, or green+CLEAN for repos configured that way), else "
     "exits 3 wait / 4 the user's call. If the user approved a merge the bot did not: `pr-ship <pr> --approved-by "
     "<ask-id>` (the chosen option must start `ship <repo>#<n>@<head sha>`). Raw `gh pr ready|merge` only on the "
     "user's explicit word for that PR."),
    (r"\bgh\s+api\b.*(comments/\d+/replies|addPullRequestReviewThreadReply|resolveReviewThread)",
     "Use `pr-reply <pr> <comment-url|comment-id|thread-id> \"<body>\" [--resolve]` to reply to (and resolve) a review thread."),
    (r"\bsleep\s+\d+.*\b(gh|pr-state)\b|\b(gh|pr-state)\b.*\bsleep\s+\d+",
     "Use `pr-state <pr> --wait ci-green|bot|approved|merged|deployed|any-change` in the background instead of "
     "a sleep/poll loop on gh."),
]
if not background:
    RULES.append((r"\bsleep\s+([2-9]\d|\d{3,})\b|\b(until|while)\b[^\n]*\bsleep\b",
     "Don't poll in the foreground: every poll is a full context re-read. Background tasks and subagents notify "
     "you when done; for PRs use `pr-state <pr> --wait ...` with run_in_background; for other conditions run the "
     "loop with run_in_background or use the Monitor tool. Short settle sleeps (<20s) are fine."))
for pat, why in RULES:
    if re.search(pat, cmd, re.S):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": why + " If the tool truly can't do it, append `# raw-ok` and say why."}}))
        break
