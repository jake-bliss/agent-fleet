"""Compacts idle leads and the first mate when their context is large, run from the watchdog tick.

The session, its status log, decisions and waits are untouched; the compact-resume hook rehydrates after /compact.
Each action goes to state/rotate.jsonl. Kill switch: rotate.json {"enabled": false} or ADW_ROTATE=0.
"""
import json
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULTS = {"enabled": True, "dry_run": False, "ctx_tokens": 100000, "max_per_tick": 2, "compact_gap_h": 3,
            "fail_gap_min": 10, "idle_s": 600, "deadline_s": 90}
EXCLUDED_MACHINES = tuple(m for m in os.environ.get("ADW_ROTATE_EXCLUDE_MACHINES", "").split(",") if m)
SKIP_ATTENTION = ("blocked", "working", "unsent", "asks")

PROBE = r'''
import glob, json, os, sys, time
pid, sid = sys.argv[1], sys.argv[2]
out = {}
try:
    out["running"] = os.path.basename(os.readlink(f"/proc/{pid}/exe").replace(" (deleted)", ""))
    st = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    out["age_s"] = float(open("/proc/uptime").read().split()[0]) - int(st[19]) / os.sysconf("SC_CLK_TCK")
except (OSError, ValueError, IndexError):
    pass
out["installed"] = os.path.basename(os.path.realpath(os.path.expanduser("~/.local/bin/claude")))
for p in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")):
    with open(p, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 524288))
        tail = f.read().decode("utf-8", "replace").splitlines()
    out["idle_s"] = time.time() - os.path.getmtime(p)
    out["idle_s"] = time.time() - os.path.getmtime(p)
    for line in reversed(tail):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("subtype") == "compact_boundary":
            out["ctx"] = (e.get("compactMetadata") or {}).get("postTokens") or 0
            break
        u = e.get("type") == "assistant" and (e.get("message") or {}).get("usage")
        if u:
            out["ctx"] = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
            break
    break
print(json.dumps(out))
'''


def config(root=None):
    cfg = dict(DEFAULTS)
    try:
        cfg.update({k: v for k, v in json.loads((Path(root or os.environ.get("ADW_HOME") or Path.home() / ".claude/adw") / "rotate.json").read_text()).items() if k in DEFAULTS})
    except (OSError, ValueError, AttributeError):
        pass
    if os.environ.get("ADW_ROTATE") == "0":
        cfg["enabled"] = False
    return cfg


def state_dir():
    return Path(os.environ.get("ADW_STATE_DIR") or Path(os.environ.get("ADW_HOME") or Path.home() / ".claude/adw") / "state")


def journal(sd=None):
    return (sd or state_dir()) / "rotate.jsonl"


def read_journal(sd=None, since=0):
    out = []
    try:
        lines = journal(sd).read_text().splitlines()[-2000:]
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("at", 0) >= since:
            out.append(e)
    return out


def record(sd, t, event, a, **extra):
    p = journal(sd)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps({"at": t, "event": event, "pane": a["pane"], "session": a["session"], "epic": a.get("epic"), **extra}) + "\n")


def version(v):
    m = re.fullmatch(r"(\d+(?:\.\d+)+)", v or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def stale(p):
    run, inst = version(p.get("running")), version(p.get("installed"))
    return bool(run and inst and run < inst)


def run_probe(mux, pane, session, timeout=30):
    out, _ = mux.hc("get", pane, "--json", timeout=max(1, timeout / 2))
    try:
        rec = json.loads(out)
    except (TypeError, ValueError):
        rec = None
    pid = ((rec or {}).get("foreground") or {}).get("pid")
    if not pid or not session:
        return None
    mach, _ = mux.split(mux.strip_self(pane))
    cmd = ["exec", *(["--on", mach] if mach else []), "--", "python3", "-c", PROBE, str(pid), session]
    out, _ = mux.hc(*cmd, timeout=max(1, timeout / 2))
    try:
        return json.loads(out)
    except (TypeError, ValueError):
        return None


def candidates(agents, mate, cfg, t):
    for a in agents:
        if a.get("kind") != "claude" or not a.get("session") or a.get("machine") in EXCLUDED_MACHINES:
            continue
        if a.get("role") != "lead" and a["pane"] != mate:
            continue
        if a["status"] not in ("idle", "done") or a.get("attention") in SKIP_ATTENTION:
            continue
        if a.get("last_active") and t - a["last_active"] < cfg["idle_s"]:
            continue
        yield a


def due(p, cfg, last, session, t):
    if p.get("idle_s") is None or p["idle_s"] < cfg["idle_s"] or (p.get("ctx") or 0) < cfg["ctx_tokens"]:
        return False
    return (t - last.get((session, "compact"), 0) >= cfg["compact_gap_h"] * 3600
            and t - last.get((session, "failed"), 0) >= cfg["fail_gap_min"] * 60)


def guard_for(mux, a):
    return mux.idle_guard(a["pane"], a["session"])


def run(agents, mate, mux, prompt, t=None, cfg=None, sd=None, probe=None, dry_run=False, deadline_s=None):
    """Compacts up to max_per_tick due terminals within the deadline; returns the actions taken (or planned)."""
    cfg = cfg or config()
    if not cfg["enabled"]:
        return []
    t = t or time.time()
    stop = time.monotonic() + (cfg["deadline_s"] if deadline_s is None else deadline_s)
    dry_run = dry_run or cfg["dry_run"]
    last = {(e["session"], e["event"]): e["at"] for e in read_journal(sd, t - 7 * 86400)}
    jobs, seen = [], {}
    for a in candidates(agents, mate, cfg, t):
        left = stop - time.monotonic()
        if len(jobs) >= cfg["max_per_tick"] or left < 5:
            break
        p = probe(mux, a["pane"], a["session"]) if probe else run_probe(mux, a["pane"], a["session"], timeout=min(30, left))
        if not p:
            continue
        seen[a["session"]] = {"pane": a["pane"], "stale": stale(p), "at": t}
        if due(p, cfg, last, a["session"], t):
            jobs.append((a, f"context {(p.get('ctx') or 0) // 1000}k"))
    if not dry_run:
        remember(sd, seen, t)
    done = []
    for a, why in jobs:
        left = stop - time.monotonic()
        if not dry_run and left < 5:
            break
        if dry_run:
            done.append({"event": "would-compact", "pane": a["pane"], "why": why})
            continue
        out, err = prompt(a["pane"], "/compact", guard=guard_for(mux, a), timeout=min(10, left))
        event = "compact" if out is not None else "failed"
        record(sd, t, event, a, why=why, detail=str(err or "")[:200])
        done.append({"event": event, "pane": a["pane"], "why": why})
    return done


def remember(sd, seen, t):
    p = (sd or state_dir()) / "rotate-seen.json"
    try:
        old = json.loads(p.read_text())
    except (OSError, ValueError):
        old = {}
    old = {k: v for k, v in {**old, **seen}.items() if t - v["at"] < 86400}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(old))


def stale_count(sd=None, t=None):
    t = t or time.time()
    try:
        return sum(1 for v in json.loads(((sd or state_dir()) / "rotate-seen.json").read_text()).values()
                   if v["stale"] and t - v["at"] < 86400)
    except (OSError, ValueError):
        return 0


def recent(sd=None, t=None, hours=24):
    t = t or time.time()
    lines = [f"- {e['event']} `{e['pane']}` [{e.get('epic') or '-'}] {e.get('why') or ''}"
             + (f" :: {e['detail']}" if e["event"] == "failed" and e.get("detail") else "")
             for e in read_journal(sd, t - hours * 3600)]
    n = stale_count(sd, t)
    return lines + ([f"- {n} session(s) run a claude binary older than the installed one"] if n else [])
