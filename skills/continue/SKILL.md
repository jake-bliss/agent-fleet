---
name: continue
description: Jump straight to one ADW Board item by its id — an adw-ask decision (d-1a2b3c), a herdr pane (w3:p4) or an epic slug — and show what it needs from the user. Triggers on "/continue <id>", "continue d-…", "continue w3:p4". Read-only.
---

# /continue <id>

1. Run `adw-board show <id>`.
2. Tell the user, in at most five lines: what the item is, what it is waiting on, and the one action that
   moves it — answer the decision card on the board, or drive the tab at
   `http://127.0.0.1:4518/#view=<pane>` (or your `tailscale serve` URL).
3. Do not act on it yourself (no prompting tabs, no answering decisions) unless the user then asks you to.

If `adw-board show` finds nothing, say so and list what it accepts (decision id, pane, epic slug).
