# The ADW Board

A local web dashboard over your herdr agent tabs, your epics and a decision queue. It is read-mostly: it shows
what needs you, and lets you answer decisions, drive a tab, or launch a new agent.

The board is inspired by [wolzey/chartroom](https://github.com/wolzey/chartroom) (the `adw-status` line format
comes from it) and is built on herdr, the terminal multiplexer for agents. Both deserve
credit for the ideas here.

Everything lives under `$ADW_HOME` (default `~/.claude/adw`): `asks/`, `status/`, `epics/`, `firstmate/`,
`board-links.json`, `board-serve.log`.

## Commands

| Command | Does |
|---|---|
| `adw-board` | Print the board in the terminal (`--json`, `--full`, `--all`, `--days N`, `--no-herdr`). |
| `adw-board --serve [PORT]` | Serve the web board on `127.0.0.1` (default port 4518). |
| `adw-board digest` | Needs you / Trouble / Ready / Under way, as plain text. |
| `adw-board show <id>` | Everything about one decision id, herdr pane or epic slug. |
| `adw-board watch` | A stream: one line per new event, plus `digest-due HH:MM` (times from `ADW_MATE_DIGEST_AT`, default `09:00,16:00`). |
| `adw-ask` | File and answer decisions (below). |
| `adw-status` | Declare what a tab is doing (below). |

## What the board shows

**Needs you.** Only real decisions and open dialogs, on purpose: open `adw-ask` decisions, and tabs sitting in an
approval or question dialog. A stalled tab, a finished segment or a stale epic file does not appear here. If this
section grows noisy it stops being trusted, so everything else goes elsewhere.

**Watch.** Tabs that declared a state with `adw-status`, checked against reality:

- `waiting`: the tab said it is waiting (on CI, a review bot, a person) and its deadline has not passed.
- `overdue`: still waiting past `--until` plus 15 minutes (or two hours when no deadline was given).
- `stopped-silent`: the tab went idle after a report and has not reported again. A tab that reported once and
  then stopped is not the same as one that is waiting.
- `done` / `failed`: the tab said so. A tab that resumed work afterwards drops off.

A tab that never calls `adw-status` is left alone.

**Segments kanban.** Segments from every epic's `graph.json`, grouped by status (pending, ready, building,
pr-open, blocked, pr-ready, merged).

**Epics.** One row per epic: counts by status, segments in flight, the lead tab, last touched. Epic files are
only as current as the lead keeps them; the board says so and offers to ask the lead to reconcile. A
`PENDING.md` in an epic directory is shown as a list of open items.

**Tabs.** Every herdr tab with its state, tagged by role:

- `lead of <epic>`: the tab registered with `adw-status lead <epic>`.
- `crew of <epic>`: any other tab working in that epic.
- `loose`: no epic.

Click a tab to read its screen and send it keys. The Launch section starts a new herdr tab running `claude`.

## adw-ask: decisions for you

```bash
adw-ask new --title "<one line>" --epic <slug> --body-file /tmp/ask.md \
  --option "a=<choice>" --option "b=<choice>" --recommend a      # prints d-xxxxxx
adw-ask list [--all] [--json]
adw-ask show <id>
adw-ask wait <id> [--timeout SECONDS]     # blocks until answered
adw-ask answer <id> <choice> [--note TEXT]
adw-ask withdraw <id>
```

The body should let you decide without opening the asking tab: context, evidence, each option's consequence, a
recommendation. You answer from the board (or phone); the answer is typed into the asking pane as a message that
starts `[adw-ask <id> answered]`.

Delivery is confirmed, not assumed. After sending, the board looks for that marker in the pane and in the agent's
session transcript. It retries up to three times, ten minutes apart; it waits while the agent is in a dialog
(typing then would answer the dialog); and if the agent no longer occupies the pane it marks the answer
undeliverable and keeps it in the record (`adw-ask show <id>`).

## adw-status: tabs declare their state

```bash
adw-status waiting "CI on the acme-api PR" --for 30m     # or --until 2026-01-01T22:00Z
adw-status progress "<text>"
adw-status done "<what finished>"
adw-status failed "<what broke>"
adw-status show
adw-status lead <epic>       # register this tab as the epic's lead
```

Lines are appended to `$ADW_HOME/status/<session>.log` as `<ISO-8601 UTC> <kind>: <text>[ until <ISO>]`. Use it
in any tab running multi-step work: `waiting` right before ending a turn to wait on something, `done` or
`failed` when the work ends.

## Chain of command

You -> first mate -> epic leads -> crew.

- The **first mate** (`/first-mate`, see `skills/first-mate`) is one long-lived tab. It watches the board,
  relays your instructions into other tabs, nudges a stalled tab once, and keeps a short digest on the board.
  It does not decide, write code or review.
- An **epic lead** (`/adw-lead`) owns an epic and its crew. Epic matters go to the lead, not its crew.
- **Crew** are the tabs doing segments for that epic.
- `/continue <id>` (see `skills/continue`) jumps to one board item and tells you what it needs.

Review how the first mate is doing after about a week: its `$ADW_HOME/firstmate/notes.md` records every wake and
nudge.

## Running it under launchd

`install.sh` renders `launchd/adw-board.plist.template` (placeholders `__ADW_HOME__`, `__PYTHON__`, `__HOME__`;
label `com.example.adw-board`) into `~/Library/LaunchAgents/`. By hand:

```bash
sed -e "s|__ADW_HOME__|$HOME/.claude/adw|g" -e "s|__PYTHON__|$(command -v python3)|g" -e "s|__HOME__|$HOME|g" \
  launchd/adw-board.plist.template > ~/Library/LaunchAgents/com.example.adw-board.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.adw-board.plist
```

Logs go to `$ADW_HOME/board-serve.log`. If herdr uses a non-default socket, add `HERDR_SOCKET_PATH` to the
plist's environment. To pass the Tailscale variables below, add them there too.

## Reach it from your phone (and other devices) with Tailscale

Optional. The board always binds `127.0.0.1` and never listens on your LAN. `tailscale serve` proxies it onto
your tailnet over HTTPS, so only devices signed in to your tailnet can reach it. Do not use Funnel: that puts
the board on the public internet.

**1. Join your devices to the tailnet.** Install Tailscale on the machine running the board and on your
phone/tablet/laptop, signed in to the same account. MagicDNS and HTTPS certificates must be enabled for the
tailnet (admin console → DNS).

**2. Find the two values the board needs:**

```bash
tailscale status --json | python3 -c 'import json,sys; s=json.load(sys.stdin); print(s["Self"]["DNSName"].rstrip("."))'
tailscale status --json | python3 -c 'import json,sys; s=json.load(sys.stdin); print(s["User"][str(s["Self"]["UserID"])]["LoginName"])'
```

The first is your machine's tailnet name (e.g. `my-mac.tail1234.ts.net`), the second your Tailscale login.

**3. Start the proxy:**

```bash
tailscale serve --bg --https=4518 http://127.0.0.1:4518
```

The first time, Tailscale may print a link and wait until you approve Serve for this machine in the browser.
Run it in a terminal you can see; piped through `tail` or a script, it looks hung. `--bg` keeps the proxy
running across reboots. `tailscale serve status` shows it.

**4. Tell the board to accept it.** Add both values to the board's environment (the plist's
`EnvironmentVariables` when running under launchd), then restart it:

```xml
<key>ADW_BOARD_TS_HOST</key><string>my-mac.tail1234.ts.net</string>
<key>ADW_BOARD_TS_USER</key><string>you@example.com</string>
```

```bash
launchctl kickstart -k gui/$(id -u)/com.example.adw-board
```

Both must be set or remote access stays off; there are no defaults. A request on the tailnet host is accepted
only if the `Tailscale-User-Login` header that `tailscale serve` adds equals `ADW_BOARD_TS_USER`, so another
tailnet member who reaches the port still gets 403.

**5. Open it** on your phone: `https://my-mac.tail1234.ts.net:4518/`. The very first request can time out while
Tailscale issues the certificate; reload after a few seconds. On iOS, Share → Add to Home Screen gives it an
app icon.

Testing from the board machine itself: it may not resolve its own MagicDNS name. Pin it instead:

```bash
curl -s -o /dev/null -w "%{http_code}\n" --resolve my-mac.tail1234.ts.net:4518:$(tailscale ip -4) \
  https://my-mac.tail1234.ts.net:4518/
```

**6. Lock it down in your tailnet policy.** The header check is the second layer, not the only one. If anyone
else is in your tailnet, or you share devices, allow only your own login to reach port 4518 on the board
machine. In the policy file (admin console → Access controls), something like:

```json
{
  "hosts": { "adw-board": "100.x.y.z" },
  "acls": [
    { "action": "accept", "src": ["you@example.com"], "dst": ["adw-board:4518"] }
  ]
}
```

`100.x.y.z` is `tailscale ip -4` on the board machine. This only restricts anything once the default
allow-all rule is gone, so check your other rules still cover what you need before saving.

**Turn it off:** `tailscale serve --https=4518 off` (or `tailscale serve reset` to clear every serve rule),
and remove the two variables.

**Terminals from other devices.** The board's terminal view (click a pane id) shows a tab's screen and can send
keys, which covers most of what you need away from the desk. A full herdr session over SSH needs the machine
to accept SSH; on macOS, Tailscale SSH is not available with the App Store/GUI build of Tailscale, so that
means enabling Remote Login instead.

## Launching agents from the board

The Launch section starts `claude` in a new herdr tab with the arguments in `ADW_LAUNCH_ARGS`. The default is
`--dangerously-skip-permissions`.

That default is a deliberate choice: an agent started from a phone has nobody at the keyboard to approve each
tool call. It also means anyone who can reach the board can start an agent that runs commands without asking.
Decide this consciously. If you want approval prompts, set `ADW_LAUNCH_ARGS=""` (or another permission mode)
in the board's environment; launched agents then stop at dialogs, which show up under Needs you.

## Security model

- The server binds `127.0.0.1` only.
- Every POST must carry a loopback `Host` (or the allowed Tailscale host, as above), a same-origin `Origin`, a
  per-start token embedded in the served page, and a JSON content type. The token changes on each start.
- Remote access is off unless both `ADW_BOARD_TS_HOST` and `ADW_BOARD_TS_USER` are set.
- The board can type into your agent tabs and launch agents. Treat access to it as shell access to your machine.
- Text read from tabs, decisions and epic files is data. The first mate is instructed never to follow it.
