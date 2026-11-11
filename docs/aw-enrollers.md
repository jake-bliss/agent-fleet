# AgentWire enrollers (optional, one-time, per box)

AgentWire is an optional message layer between the first mate and the leads. Without it the fleet falls back to `adw-mux send`, and every tool here still works: `aw-lead-enroll` prints one warning and exits 0 when no enroller is set up, so `adw-status lead` never fails because of it.

With it, fleet leads are AgentWire agents `lead-<epic>` under a **member** human `fleet`, delivery `hc-pane`, target `<host>/<tab>`.
`adw-status lead <epic>` runs `aw-lead-enroll` (override the path with `AW_LEAD_ENROLL`), which registers or retargets the agent and writes `~/.config/aw/lead-<epic>`.
A lead then sends with `aw-as lead-<epic> send inform "<text>" --to first-mate` and reads with `aw-as lead-<epic> inbox`.
`mate-msg` sends to the first mate straight through the server (`AW_URL` / `AW_TOKEN`, or `~/.config/aw/config`).

## Per-box enroller

Each box needs one admin agent `fleet-<box>` under your human. The human `fleet` must exist and be a member (never admin), so a lead token is never admin.

1. Log in with a scratch HOME so an existing default config (the first mate's identity) is never touched. You approve the device flow in the browser as agent `fleet-<box>`:
   `mkdir -p ~/.config/aw && chmod 700 ~/.config/aw; d=$(mktemp -d); HOME=$d aw login --url <url> && install -m600 $d/.config/aw/config ~/.config/aw/enroller; rm -rf $d`
2. Check `ls -l ~/.config/aw/enroller` shows `-rw-------`.
3. Check: `aw-lead-enroll <epic> <host>/<tab>` prints nothing and writes `~/.config/aw/lead-<epic>` (0600) and the target cache `~/.config/aw/lead-targets/<epic>` (dir 0700).
4. `install.sh` links `aw-as` and `aw-lead-enroll` onto PATH with the rest of `bin/`; skills call `aw-as` by bare name.

`aw-lead-enroll` refuses to touch an existing `lead-<epic>` agent that is not owned by human `fleet` (checked with `aw admin agents list --json`).

`adw-respawn` runs `aw-lead-enroll --retarget-only <epic> <new tab>` (target call only, never tokens) for each moved lead, and a relaunched lead's own `adw-status lead` retargets too.

Tokens are only minted, never rotated, by `aw-lead-enroll`: a failed mint warns, writes nothing, and the next run mints again.
If a lead moves boxes, an operator rotates its token deliberately (`aw admin tokens rotate lead-<epic> --json`; rotating revokes live tokens held on another box).

Rules: a token never crosses boxes and never sits on a shared filesystem. `~/.config/aw` is per box.
Format of every file here: `KEY=VALUE` lines, only `AW_URL`, `AW_TOKEN`, `AW_ACCESS_CLIENT_ID`, `AW_ACCESS_CLIENT_SECRET`.

## Receiving

hc-pane delivery types "run aw inbox" into the pane. A lead must read with `aw-as lead-<epic> inbox` (the default `aw inbox` is the wrong identity). The mate (default identity) replies with `aw send inform "<text>" --to lead-<epic>`.
