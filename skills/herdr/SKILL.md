---
name: herdr
description: "Control Herdr, a terminal multiplexer for coding agents, via the `herdr` CLI. Use only when the user explicitly mentions Herdr or asks to inspect/control panes, tabs, workspaces, commands, or another agent through it. Not for generic background terminals or delegation. Requires HERDR_ENV=1."
---

# Herdr

Herdr organizes terminals into workspaces, tabs, and panes, recognizes coding agents in panes, and exposes the session via the `herdr` CLI.

Before any control command, verify you are in a Herdr-managed pane:

```bash
test "${HERDR_ENV:-}" = 1
```

If it fails, say you are not running inside Herdr and stop. Do not inspect or control the focused Herdr session from outside Herdr.

Read `references/concepts.md` for IDs, lifecycle states, wait semantics, read sources, and the alternate-screen fallback.

## Learn the CLI

The installed binary is the authority. Start with `herdr --help`, then print a group by running it without a subcommand: `herdr agent|pane|workspace|tab|worktree|terminal|notification|integration|session`.

Do not run bare `herdr` (launches/attaches the TUI). Do not probe a mutating nested command by omitting arguments (`herdr workspace create` executes with defaults). Control commands return JSON; read IDs and state from responses, never predict them.

## Primitives

- Workspace/tab/pane = layout. Pane commands = raw terminals, shells, tests, servers, I/O. Agent commands = the recognized agent in a pane (use when Herdr must validate identity or interpret `idle`/`working`/`blocked`/`done`/`unknown`).
- Agent commands take a unique live agent name or the hosting pane ID (not terminal IDs or kind labels).
- Prefer `--current` to target the calling pane; omitting a target may use the UI-focused pane, which can belong to the user or another client.
- Discover state: `herdr workspace list`, `herdr tab list --workspace "$HERDR_WORKSPACE_ID"`, `herdr pane current --current`, `herdr pane list --workspace "$HERDR_WORKSPACE_ID"`, `herdr agent list`.

## Start and coordinate an agent

Default to a sibling pane in the current tab and cwd. No new workspace, tab, worktree, or cwd unless the user explicitly asks. Honor a requested direction; otherwise inspect `herdr pane layout --pane "$HERDR_PANE_ID"`, split a wide pane right and a narrow/tall pane down, and avoid repeated same-direction splits. Keep the user's focus and preserve cwd:

```bash
herdr pane split --current --direction right --cwd "$PWD" --no-focus
```

Read the new pane ID from `.result.pane.pane_id`. `agent start` needs an available shell pane at its interactive prompt (shell in foreground, nothing else running):

```bash
herdr agent start reviewer --kind codex --pane <returned-pane-id>
herdr agent start reviewer --kind codex --pane <returned-pane-id> -- <agent-args...>
```

Use the kind the user requested (`herdr agent` lists kinds). Native agent args only after `--`. Submit work:

```bash
herdr agent prompt reviewer "Review the current diff and report only actionable findings." --wait --timeout 120000
herdr agent wait reviewer --until blocked --timeout 120000   # only for state-specific waits
herdr agent send-keys reviewer esc
herdr agent send-keys reviewer ctrl+c
herdr agent get reviewer
herdr agent read reviewer --source recent-unwrapped --lines 120
```

If a wait fails or returns `blocked`, inspect `agent get` and `agent read` before deciding what input to send. Use the pane surface only when raw terminal control is intentional.

## Run an ordinary command in another pane

Split a sibling pane as above, read `.result.pane.pane_id`, then:

```bash
herdr pane run <returned-pane-id> "just test"
herdr pane wait-output <returned-pane-id> --match "test result" --timeout 120000   # or --regex <pattern>
herdr pane read <returned-pane-id> --source recent-unwrapped --lines 120
```

Read sources: `visible`, `recent`, `recent-unwrapped` (prefer for logs), `detection`. If more `--lines` reveals nothing more, see the alternate-screen fallback in `references/concepts.md`.

## Safety and coordination rules

- Use `--no-focus` for background work unless the user asked to switch context.
- Use `--current`, an explicit pane ID, or a unique agent name. Do not rely on another client's focused pane.
- Parse IDs from JSON responses. Do not derive them from sidebar order or examples.
- Do not close workspaces, tabs, panes, or sessions you did not create unless the user explicitly asked.
- Never run `herdr server stop` from an active session unless the user explicitly intends to stop the server and its pane processes.
- Never kill the main Herdr process. Use named test sessions for experiments that need an isolated server.
- CLI server errors are JSON on stderr with exit status 1; syntax errors exit 2.
