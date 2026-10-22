# Several machines, one board

By default everything runs on one machine and none of this applies. With nothing configured, the board,
`adw-ask` and `adw-status` behave exactly as a single-machine setup.

## Shape

One **hub** runs the board and the first mate. Other boxes are **nodes**: agents run there in their own herdr,
and the hub reaches each node's herdr over a forwarded socket. A tab on any machine shows up on the one board.

```
hub:   adw-board, first mate, $ADW_HOME  (asks, status, epics)
node1: agents + their own herdr          <- hub reaches it at ~/.config/herdr/remote/node1.sock
```

## Configure the hub

1. Name the other machines: `export ADW_MACHINES="laptop,node1"`, or put `{"machines": ["laptop", "node1"]}` in
   `$ADW_HOME/machines.json` (`config/machines.example.json`). Names are single words (`\w+`).
2. For each name, make that machine's herdr socket appear at `~/.config/herdr/remote/<name>.sock` on the hub
   (override the directory with `ADW_HERDR_REMOTE_DIR`). Forward it over ssh, for example
   `ssh -N -L ~/.config/herdr/remote/node1.sock:<path to herdr.sock on the node> <node>`; keep the tunnel
   up with whatever supervisor you use. A name whose socket is absent is skipped, so a node that is off
   never breaks the board.
3. On each node set `ADW_MACHINE=<its own name>` in the agents' environment. The board lists panes of a
   node as `<name>/<pane id>`, and `adw-ask` / `adw-status` record the asking pane that way, so an answer is
   routed to the right machine. Leave `ADW_MACHINE` unset on the hub; its panes keep bare ids.
4. Agents on a node must read and write the same `$ADW_HOME` as the hub, because asks, status and epic
   state are files there. Share it with a network mount (optional; any shared filesystem works), and share
   `~/.claude/projects` too if the board should read transcripts of node sessions.

## What becomes machine-aware

- `adw-board`: lists agents from every live remote, labels tabs per machine (a tab id is only unique per
  machine), and routes prompts, key presses and focus to the owning machine's socket. Each agent row carries
  a `machine` field.
- `adw-ask` and `adw-status`: record `<machine>/<pane>` when `ADW_MACHINE` is set, and deliver answers through
  the matching socket.
- Pane ids accepted by the board's API may carry the `<name>/` prefix.
