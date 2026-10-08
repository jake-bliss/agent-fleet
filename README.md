# adw

`adw` is a way of working with coding agents, plus the scripts and skills that make it run. One
command (`/adw`) takes a written brief to a merged PR: a strong model orchestrates, a cheaper model
builds in an isolated git worktree, two models from different vendors review, the repo's full CI runs
locally before anything is pushed, and a pre-push hook refuses pushes that skipped any of that. A
second command (`/adw-lead`) plans an epic into a dependency graph of such segments and dispatches
them, stopping only at the decisions you have to make. A small local web board shows every agent tab,
every epic and every open decision, and lets you answer those decisions from your phone.

This is a personal setup shared as-is. It reflects one person's repos, models and habits. Adapt it
rather than adopt it: read `docs/way-of-working.md`, take the parts that fit, and change the rest.

## Who it's for

People already running many Claude Code (and Codex) agents at once in
herdr, who want those agents to carry work to a reviewed,
merged PR without being supervised turn by turn — and who want one place to see what needs them.

## Requirements

- macOS for `cow-worktree`'s copy-on-write cloning (APFS `clonefile`); elsewhere it falls back to a
  plain `git worktree add`, and `--launchd` is macOS-only
- Python 3.11+ (stdlib only)
- git, the GitHub CLI (`gh`), herdr, Claude Code
- Claude Code subagents named `worker` (builds in a given worktree) and `reviewer` (read-only defect
  hunt); the skills dispatch them by name, so define your own in `~/.claude/agents/` (an example
  `worker` is in `agents/worker.md`)
- Codex CLI — optional, but the cross-model review assumes it
- Tailscale — optional, to reach the board from your phone and other devices (setup in `docs/board.md`)

## Quick start

```bash
git clone <this repo> ~/src/adw && cd ~/src/adw
./install.sh                    # links commands into ~/.local/bin, skills into ~/.claude/skills,
                                # creates $ADW_HOME (default ~/.claude/adw), links review/, board/
                                # and docs/ into it, and copies example configs
$EDITOR ~/.claude/adw/manifest.json   # list your repos: path, base branch, verify commands
$EDITOR ~/.claude/adw/ci-parity.json  # each repo's CI jobs, mirrored by ci-preflight
adw-board --serve 4518          # or ./install.sh --launchd to keep it running
```

Paste what you want from `docs/CLAUDE.snippet.md` into `~/.claude/CLAUDE.md`. Install the pre-push
gate in each repo you want guarded (`install-prepush-gate`, see `docs/ci-gate.md`). Then, in a herdr
tab inside Claude Code:

```
/adw-lead orders "Add partial refunds to the orders API"
```

or, for a single scoped change, `/adw <repo-key> <brief.md>`. Open http://127.0.0.1:4518/ to watch.

## Map

| Path | What |
|---|---|
| `skills/*/SKILL.md`, `skills/*/references/` | each skill's operative rules; the rationale behind them sits in `references/` |
| `skills/adw/` | `/adw` — one segment to a merged PR |
| `skills/adw-lead/` | `/adw-lead` — the epic conductor |
| `skills/first-mate/`, `skills/continue/` | the fleet overseer tab, and jumping to one board item |
| `board/` | the ADW Board, `adw-ask` (decision queue), `adw-status` (tab state) |
| `bin/` | `cow-worktree`, `ci-preflight`, `review-receipt`, the pre-push gate, `adw-lead-state`, pruning |
| `review/` | the shared review contract and the lenses reviewers apply |
| `config/` | example configs, copied into `$ADW_HOME` by the installer |
| `launchd/` | the board's launchd template |
| `agents/worker.md` | an example worktree-confined builder subagent |
| `docs/way-of-working.md` | the whole method in two pages — start here |
| `docs/board.md` | the fleet layer: board, decisions, status, first mate |
| `docs/ci-gate.md` | local CI parity and the pre-push gate |
| `docs/worktrees.md` | copy-on-write worktrees |
| `docs/codex-overnight.md` | handing Codex unattended overnight builds: cutting, the goal prompt, the morning handoff |
| `docs/CLAUDE.snippet.md` | conventions to paste into your own `CLAUDE.md` |

Every path honours `ADW_HOME` (default `$HOME/.claude/adw`). Real config lives there and is never
committed.

## Credits

- [wolzey/chartroom](https://github.com/wolzey/chartroom) — the first-mate / XO idea and the status
  event format `adw-status` writes.
- herdr — the terminal multiplexer for agents the fleet layer is
  built on.

MIT licensed; see `LICENSE`.
