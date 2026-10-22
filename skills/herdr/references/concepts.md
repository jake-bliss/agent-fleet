# Herdr concepts and read details

## Layout, panes, agents
- Workspace, tab, and pane topology organize terminal locations.
- Pane commands control raw terminals, shells, tests, servers, input, and output.
- Agent commands control the recognized coding agent currently occupying a pane.
- A pane exists whether or not it contains an agent. `agent start` requires an existing available shell pane and never creates, splits, or moves layout.
- Agent commands accept either a unique live agent name or the pane ID currently hosting that agent. They do not accept terminal IDs or bare agent-kind labels. Names must match `[a-z][a-z0-9_-]{0,31}` and be unique among live agents. A name follows the current pane occupant and is cleared when that agent exits, is released, or is replaced.

## Lifecycle states
- `idle`: agent ready for input and its tab has been seen in the focused Herdr UI.
- `done`: same underlying idle state after unseen background work finishes. Focusing the tab or targeting the pane/agent with a focus command marks it seen. CLI reads do not mark it seen.
- `blocked`: Herdr recognized an approval or question UI.
- `unknown`: an agent is present but Herdr cannot classify it confidently; it does not prove completion.

## IDs
Public IDs are opaque stable handles: workspace `w1`, tab `w1:t1`, pane `w1:p1`.
- Closed tab and pane IDs are not reused.
- A pane moved into another workspace receives a new workspace-qualified pane ID. After `pane move`, continue with `.result.move_result.pane.pane_id` or the live agent name. The old value is `.result.move_result.previous_pane_id`; only the moved process's inherited caller context keeps resolving it, so do not use it as a general agent target.
- Herdr injects caller context into each managed pane: `printf '%s\n' "$HERDR_WORKSPACE_ID" "$HERDR_TAB_ID" "$HERDR_PANE_ID"`.
- Creation responses: `workspace create` returns `.result.workspace`, `.result.tab`, `.result.root_pane`. `tab create` returns `.result.tab` and `.result.root_pane`. `pane split` returns `.result.pane`.

## Agent prompt/wait semantics
- `agent start` returns only after Herdr detects the expected agent in the same pane and considers it ready for input (default 30s startup timeout).
- `agent prompt` atomically submits text and encoded Enter while honoring the pane's live bracketed-paste mode. `--wait` waits for the first settled `idle`, `done`, or `blocked`; do not repeat those defaults with `--until`.
- A prompt sent from a non-working state must produce an observed lifecycle change within five seconds, otherwise Herdr returns `agent_prompt_stalled`. The wait tracks lifecycle state, not an individual turn; if the agent is already working, completion of the active turn may satisfy it.
- Use `--until` only for a state-specific workflow, e.g. `herdr agent wait reviewer --until blocked --timeout 120000`. Without `--until`, standalone `agent wait` uses the same settled-state defaults.
- `herdr agent send-keys` validates all keys before writing any bytes.

## Pane run / read
- `pane run` atomically sends command text and Enter.
- `pane wait-output` searches the selected snapshot immediately, so existing output can match. `--match <text>` is a literal substring; `--regex <pattern>` is a Rust regex. Omitting `--timeout` waits indefinitely.
- Read sources: `visible` (rendered viewport); `recent` (recent rendered output incl. soft wraps); `recent-unwrapped` (soft wraps joined; prefer for logs/transcripts); `detection` (plain-text bottom-buffer snapshot used for agent detection).
- Use `--format ansi` when colors/styling are evidence; otherwise text.

## Alternate-screen fallback
`--lines` asks for more rows from the pane's screen and host scrollback. If raising it does not reveal more of a completed response, the pane is probably running the agent on the terminal's alternate screen; rows that leave it never enter host scrollback, so a larger count cannot recover them. Then ask the agent to write its complete response as Markdown in a temporary directory and reply only with the file path, and read that file directly. Fallback only; do not request file output in the initial prompt.
