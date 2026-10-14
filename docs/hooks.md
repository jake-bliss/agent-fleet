# Hooks

Claude Code hooks that make the habits in `docs/CLAUDE.snippet.md` happen every time instead of usually.
`install.sh` links this directory to `$ADW_HOME/hooks`; it does not register anything and never edits
`~/.claude/settings.json`. Add the entries below yourself.

| Hook | Event | Does |
|---|---|---|
| `tool-adoption.py` | `PreToolUse` on `Bash` | denies the raw form of a command that has a purpose-built tool (`gh pr checks`, `gh pr merge`, `sleep` loops, read-only `codex exec`, ...) and names the tool. Append `# raw-ok` plus a reason to a command to override |
| `model-pin.py` | `PreToolUse` on `Agent` | refuses a non-Sonnet model for `worker` / `adw-conductor` and a non-haiku model for `Explore`. A `# model-ok <reason>` line in the prompt overrides |
| `cross-model-review.py` | `UserPromptSubmit` | when a prompt asks for a review or a plan critique, injects the instruction to run Claude and Codex both and adjudicate. Silent if `codex` is not installed; reads `ADW_HOME` |
| `herdr-agent-state.sh` | session events | herdr's own integration file, shipped for reference. herdr writes and regenerates it when you install its Claude integration; prefer that over registering this copy |

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
    "UserPromptSubmit": [
      { "matcher": "*",
        "hooks": [{ "type": "command", "command": "python3 $ADW_HOME/hooks/cross-model-review.py", "timeout": 5 }] }
    ]
  }
}
```

Each hook exits 0 with no output on any failure, so a broken hook never blocks a prompt or a tool call.
The `Agent` matcher is the subagent tool; on an older Claude Code it is named `Task`.
