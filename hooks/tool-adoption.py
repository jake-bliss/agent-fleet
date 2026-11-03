#!/usr/bin/env python3
import json, re, shlex, sys

try:
    payload = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
inp = payload.get("tool_input", {})
cmd = inp.get("command", "")
background = bool(inp.get("run_in_background"))
permission_mode = payload.get("permission_mode", "")

def args_after(pat):
    m = re.search(pat + r"\s*(.*?)\s*(?:$|[;&|])", cmd, re.S)
    return m.group(1).strip() if m else ""


def pr_num():
    m = re.search(r"\bgh\s+pr\s+\w+\s+(?:\S+\s+)*?(\d+)\b", cmd)
    return m.group(1) if m else "<pr>"


def emit(decision, **extra):
    out = {"hookEventName": "PreToolUse", "permissionDecision": decision}
    out.update(extra)
    print(json.dumps({"hookSpecificOutput": out}))
    sys.exit(0)


SEPARATORS = r"&&|\|\||[;&|\n]"

if "# raw-ok" in cmd:
    sys.exit(0)

m = re.fullmatch(r"\s*gh\s+pr\s+checks\s+(\d+)((?:\s+(?:--watch|-w|(?:-R|--repo)(?:\s+|=)[\w.-]+/[\w.-]+))*)\s*", cmd)
if m:
    num, flags = m.group(1), m.group(2)
    repo = re.search(r"(?:-R|--repo)(?:\s+|=)(\S+)", flags)
    base = "pr-state %s%s" % (num, " -R " + repo.group(1) if repo else "")
    if re.search(r"--watch|-w\b", flags):
        new_cmd, bg = base + " --wait ci-green", True
    else:
        new_cmd, bg = base, background
    emit("allow", updatedInput=dict(inp, command=new_cmd, run_in_background=bg),
         additionalContext="Rewritten `%s` to `%s`%s." % (cmd.strip(), new_cmd, " (backgrounded)" if bg else ""))

RULES = [
    (r"\bcodex\s+exec\b(?=.*(read-only|\breview\b|critique))",
     lambda: "Use: codex-review diff <worktree> [--base origin/main] [--since <sha>] (or `codex-review plan <repo> <file>`, "
             "`codex-review ask <dir> \"<q>\"`) instead of a hand-built read-only `codex exec`."),
    (r"\bgh\s+pr\s+checks\b",
     lambda: "Use: pr-state %s (snapshot) or `pr-state %s --wait ci-green` with run_in_background instead of "
             "`gh pr checks`." % (pr_num(), pr_num())),
    (r"\bgh\s+pr\s+view\b.*--json\s+\S*(statusCheckRollup|reviewDecision|mergeStateStatus|mergeable|mergeCommit)",
     lambda: "Use: pr-state %s --json (CI/review/merge state and merge commit) instead of `gh pr view --json ...`." % pr_num()),
    (r"\bgh\s+run\s+(watch|list|view)\b",
     lambda: "Use: pr-state --run <id> [--failures] for one Actions run, `pr-state --runs [--branch B] [--event E] "
             "[--sha <full sha>] [--limit N]` to list runs, `pr-state <pr> --failures` for a PR head, and "
             "`pr-state <pr> --wait deployed` (backgrounded) to watch post-merge deploys."),
    (r"\bgh\s+api\b(?!.*(replies|\bmutation\b|resolveReviewThread|addPullRequestReviewThreadReply|\s-X\s|--method|--input))"
     r".*(pulls/\d+/comments|reviewThreads)",
     lambda: "Use: pr-state <pr> --findings all (reviewer-bot inline findings: path:line, body, url, replied)."),
    (r"\bgh\s+pr\s+(ready\b(?!.*--undo)|merge\b)",
     lambda: "Use: pr-ship %s [-R repo] [--dry-run] (marks a green draft ready or squash-merges only when the merge "
             "rules hold: ours, default base, CI green, bot-approved on head, or green+CLEAN for repos configured that way; "
             "else exits 3 wait / 4 the user's call). If the user approved a merge the bot did not: "
             "`pr-ship %s --approved-by <ask-id>` (the chosen option must start `ship <repo>#<n>@<head sha>`). Raw "
             "`gh pr ready|merge` only on the user's explicit word for that PR." % (pr_num(), pr_num())),
    (r"\bgh\s+api\b.*(comments/\d+/replies|addPullRequestReviewThreadReply|resolveReviewThread)",
     lambda: "Use: pr-reply <comment-url|id|PRRT_id> \"<body>\" [--resolve] to reply to (and resolve) a review thread."),
    (r"\bsleep\s+\d+.*\b(gh|pr-state)\b|\b(gh|pr-state)\b.*\bsleep\s+\d+",
     lambda: "Use: pr-state <pr> --wait ci-green|bot|approved|merged|deployed|any-change (run_in_background) "
             "instead of a sleep/poll loop on gh."),
]
for pat, why in RULES:
    if re.search(pat, cmd, re.S):
        emit("deny", permissionDecisionReason=why() + " If the tool truly can't do it, append `# raw-ok` and say why.")

PROBES = {"sleep", "pr-state", "curl", "test", "[", "[[", "grep", "true", "false"}


def safe_curl(seg):
    try:
        toks = shlex.split(seg)
    except ValueError:
        return False
    i = 1
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else ""
        if t in ("-X", "--request"):
            if nxt.upper() not in ("GET", "HEAD"):
                return False
            i += 2
            continue
        if t in ("-o", "--output"):
            if nxt != "/dev/null":
                return False
            i += 2
            continue
        if t.startswith("--request="):
            if t.split("=", 1)[1].upper() not in ("GET", "HEAD"):
                return False
        elif t.startswith("--output="):
            if t != "--output=/dev/null":
                return False
        elif t.startswith("--"):
            if re.match(r"--(data|form|upload-file|json|remote-name|config|variable)", t):
                return False
        elif t.startswith("-X") and len(t) > 2 and "=" not in t and re.fullmatch(r"-X[A-Za-z]+", t):
            if t[2:].upper() not in ("GET", "HEAD"):
                return False
        elif t.startswith("-") and len(t) > 1:
            if re.search(r"[dFTOXoKJ]", t[1:]):
                return False
        i += 1
    return True


def pure_wait(c):
    if re.fullmatch(r"\s*sleep\s+\d+\s*", c):
        return True
    c = re.sub(r"(?:&>|\d?>)\s*/dev/null|2>&1", "", c)
    if not re.match(r"\s*(until|while)\b", c) or re.search(r"<|\$\(|`|>", c):
        return False
    if len(re.findall(r"\bdone\b", c)) != 1 or not re.search(r"\bdone\s*$", c):
        return False
    for seg in re.split(SEPARATORS, c):
        seg = re.sub(r"^\s*(?:(?:until|while|do|then|!)\s+)*", "", seg).strip()
        if not seg or seg == "done":
            continue
        word = seg.split()[0]
        if word not in PROBES or (word == "curl" and not safe_curl(seg)):
            return False
    return True


if not background and re.search(r"\bsleep\s+([2-9]\d|\d{3,})\b|\b(until|while)\b[^\n]*\bsleep\b", cmd, re.S):
    if pure_wait(cmd):
        emit("allow" if permission_mode == "bypassPermissions" else "ask",
             updatedInput=dict(inp, run_in_background=True),
             additionalContext="This long wait was run with run_in_background: true; you will be notified when it exits. "
                               "Do not poll its output. For PR state prefer `pr-state <pr> --wait ci-green|bot|merged`.")
    emit("deny", permissionDecisionReason="Don't poll in the foreground: every poll is a full context re-read. Use: "
         "pr-state <pr> --wait ci-green|bot|merged (run_in_background), or run the bare wait (`sleep N`, or an "
         "until/while loop that only sleeps or probes) on its own with run_in_background, not chained with other "
         "commands. Short settle sleeps (<20s) are fine. If the tool truly can't do it, append `# raw-ok` and say why.")
