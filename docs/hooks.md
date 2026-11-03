# Hooks

Claude Code hooks that make the habits in `docs/CLAUDE.snippet.md` happen every time instead of usually.
`install.sh` links this directory to `$ADW_HOME/hooks`; it does not register anything and never edits
`~/.claude/settings.json`. Add the entries below yourself.

| Hook | Event | Does |
|---|---|---|
| `tool-adoption.py` | `PreToolUse` on `Bash` | denies the raw form of a command that has a purpose-built tool (`gh pr checks`, `gh pr merge`, `sleep` loops, read-only `codex exec`, ...) and names the tool; a pure `sleep` or probe loop is rewritten to run in the background (allowed only in bypass-permissions mode, else you are asked). Append `# raw-ok` plus a reason to a command to override |
| `model-pin.py` | `PreToolUse` on `Agent` | refuses a non-Sonnet model for `worker` / `adw-conductor` (Opus is allowed for an `adw-conductor` whose prompt says "redesign") and a non-haiku model for `Explore`. A `# model-ok <reason>` line in the prompt overrides |
| `cross-model-review.py` | `UserPromptSubmit` | when a prompt asks for a review or a plan critique, injects the instruction to run Claude and Codex both and adjudicate. Silent if `codex` is not installed; reads `ADW_HOME` |
| `compact-resume.py` | `SessionStart` (matcher `compact`) | after an auto-compact, prints a resume card read from disk: branch and uncommitted files, this session's open and recently answered `adw-ask` decisions, its last status lines, the epic it leads. Reads only files ADW keeps |
| `hc-session-state.sh` | `SessionStart`, `SessionEnd` | under the herdr app (`hc`) backend, records each terminal's Claude session id in `$ADW_HOME/hc-sessions/<machine>/<terminal>` so the board and `adw-respawn` can map a terminal to its session. Pass `start` or `end` as the argument |
| `herdr-agent-state.sh` | session events | herdr's own integration file (v11), shipped for reference. herdr writes and regenerates it when you install its Claude integration; prefer that over registering this copy |

## settings.json

Merge into the `hooks` block of `~/.claude/settings.json`, keeping any entries already there. Replace
`$ADW_HOME` with its absolute path (the file does not expand it):

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "python3 $ADW_HOME/hooks/tool-adoption.py" }] },
      { "matcher": "Agent",
        "hooks": [{ "type": "command", "command": "python3 $ADW_HOME/hooks/model-pin.py" }] }
    ],
    "SessionStart": [
      { "matcher": "compact",
        "hooks": [{ "type": "command", "command": "python3 $ADW_HOME/hooks/compact-resume.py" }] },
      { "matcher": "*",
        "hooks": [{ "type": "command", "command": "sh $ADW_HOME/hooks/hc-session-state.sh start" }] }
    ],
    "SessionEnd": [
      { "matcher": "*",
        "hooks": [{ "type": "command", "command": "sh $ADW_HOME/hooks/hc-session-state.sh end" }] }
    ],
    "UserPromptSubmit": [
      { "matcher": "*",
        "hooks": [{ "type": "command", "command": "python3 $ADW_HOME/hooks/cross-model-review.py", "timeout": 5 }] }
    ]
  }
}
```

Each hook exits 0 with no output on any failure, so a broken hook never blocks a prompt or a tool call.
The `Agent` matcher is the subagent tool; on an older Claude Code it is named `Task`.
