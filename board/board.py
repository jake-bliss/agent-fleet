#!/usr/bin/env python3
"""adw-board: one view of what every epic and agent tab needs from you.

Usage: adw-board needs                                  the Needs-you list, one item per line
       adw-board [--days N] [--all] [--full] [--json] [--no-herdr]
       adw-board --serve [PORT]                      web board on 127.0.0.1 (default 4518)
       adw-board digest | show <decision id | pane | epic> | watch

Env: ADW_HOME, ADW_LAUNCH_ARGS (args for agents launched from the board),
     ADW_BOARD_TS_HOST + ADW_BOARD_TS_USER (optional tailscale serve access), ADW_MATE_DIGEST_AT,
     ADW_LEAD_REFRESH_AT (context tokens past which an idle refreshable lead is flagged, default 150000),
     ADW_LEAD_REFRESH_LONG_WAIT_AT (same, for a wait of an hour or more, default 120000),
     ADW_DESK_TZ (IANA zone for the watchdog morning summary, default UTC), ADW_DESK_HOURS (start-end local hours, default 8-17),
     ADW_MACHINES (comma-separated names of other machines whose herdr is reachable at ~/.config/herdr/remote/<name>.sock), ADW_MACHINE (this machine's own name),
     ADW_MATE_IGNORE_CWDS (comma-separated cwd prefixes the first mate ignores), ADW_DISK_ALERT_PCT (default 80)
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import fcntl
import gzip
import hashlib
import importlib.util
import io
import secrets
import shutil
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

ADW_HOME = Path(os.environ.get("ADW_HOME") or Path.home() / ".claude/adw")
EPICS = ADW_HOME / "epics"
IN_FLIGHT = ("building", "pr-open")
SEGMENT_STATUSES = ("building", "pr-open", "pr-ready", "in_progress", "ready", "pending", "queued", "blocked", "held", "parked")
NEEDS_YOU = ("blocked", "pr-ready")
PENDING_STALE_DAYS = 3
PENDING_SHOWN = 5
ASK_FRESH_HOURS = float(os.environ.get("ADW_BOARD_ASK_FRESH_HOURS", "2"))
ANSWERED_SHOWN_HOURS = 24
LINKS = ADW_HOME / "board-links.json"


def load_links():
    try:
        return json.loads(LINKS.read_text()).get("sessions", {})
    except (OSError, ValueError):
        return {}


def save_link(session, epic):
    links = load_links()
    if epic:
        links[session] = epic
    else:
        links.pop(session, None)
    tmp = LINKS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"sessions": links}, indent=2))
    tmp.replace(LINKS)


def accepts_gzip(header):
    q = {}
    for part in header.split(","):
        coding, *params = [x.strip().lower() for x in part.split(";")]
        weight = 1.0
        for param in params:
            if param.startswith("q="):
                try:
                    weight = float(param[2:])
                except ValueError:
                    weight = 0.0
        q.setdefault(coding, weight)
    return q.get("gzip", q.get("*", 0.0)) > 0


def load_links():
    try:
        return json.loads(LINKS.read_text()).get("sessions", {})
    except (OSError, ValueError):
        return {}


def save_link(session, epic):
    links = load_links()
    if epic:
        links[session] = epic
    else:
        links.pop(session, None)
    tmp = LINKS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"sessions": links}, indent=2))
    tmp.replace(LINKS)


SNOOZES = ADW_HOME / "board-snoozes.json"
SNOOZE_KEY = re.compile(r"(d:d-[0-9a-f]{6}|p:[A-Za-z0-9:_-]{1,64})")


SNOOZE_LOCK = threading.Lock()


def load_snoozes():
    try:
        raw = json.loads(SNOOZES.read_text())
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {k: v for k, v in raw.items() if isinstance(v, (int, float)) and v > now}


def set_snooze(key, until):
    if not SNOOZE_KEY.fullmatch(key):
        raise ValueError("bad snooze key")
    with SNOOZE_LOCK:
        sn = load_snoozes()
        if until:
            sn[key] = int(until)
        else:
            sn.pop(key, None)
        tmp = SNOOZES.with_name(f".{SNOOZES.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(sn, indent=2))
        tmp.replace(SNOOZES)


PINS = ADW_HOME / "board-pins.json"
PIN_KEY = None


def load_pins():
    try:
        raw = json.loads(PINS.read_text())
    except (OSError, ValueError):
        return []
    return [k for k in raw if isinstance(k, str)] if isinstance(raw, list) else []


def set_pin(key, on):
    if not PIN_KEY.fullmatch(key or ""):
        raise ValueError("bad pin key")
    with SNOOZE_LOCK:
        pins = [k for k in load_pins() if k != key] + ([key] if on else [])
        tmp = PINS.with_name(f".{PINS.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(pins, indent=2))
        tmp.replace(PINS)


def snooze_until(choice):
    now = time.time()
    if choice == "tomorrow":
        t = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
        nxt = t.timestamp() if t.timestamp() > now + 3600 else t.timestamp() + 86400
        return nxt
    mins = {"1h": 60, "4h": 240}.get(choice)
    if not mins:
        raise ValueError("snooze for 1h, 4h or tomorrow")
    return now + mins * 60


def snoozed(b, key):
    return key in (b.get("snoozes") or {})


def dialog_key(a):
    return "p:" + (a.get("session") or a["pane"])

_spec = importlib.util.spec_from_file_location("asks", Path(__file__).resolve().parent / "asks.py")
asks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(asks)
_spec = importlib.util.spec_from_file_location("adw_status", Path(__file__).resolve().parent / "status.py")
adw_status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adw_status)
PIN_KEY = re.compile(r"(s:[A-Za-z0-9_-]{1,64}|p:(?:(?:\w+/)?w\w+:p\w+|" + asks.mux.PANE_PATTERN + "))")
_spec = importlib.util.spec_from_file_location("terminals", Path(__file__).resolve().parent / "terminals.py")
terminals = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(terminals)
terminals.mux = asks.mux
LIVE = None
_spec = importlib.util.spec_from_file_location("reqs", Path(__file__).resolve().parent / "reqs.py")
reqs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reqs)

LAUNCH_ARGS = shlex.split(os.environ.get("ADW_LAUNCH_ARGS", "--dangerously-skip-permissions"))
SPAWNED_SETTINGS = ADW_HOME / "spawned-settings.json"


def launch_args():
    return [*LAUNCH_ARGS, "--settings", str(SPAWNED_SETTINGS)] if SPAWNED_SETTINGS.is_file() else list(LAUNCH_ARGS)


_events = None


def events_mod():
    global _events
    if _events is None:
        spec = importlib.util.spec_from_file_location("adw_events", Path(__file__).resolve().parent / "events.py")
        _events = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_events)
    return _events


def event_state():
    try:
        ev = events_mod()
        db = ev.connect()
        try:
            return ev.open_interrupts(db), ev.last_events(db), ev.cleared_interrupts(db)
        finally:
            db.close()
    except Exception:
        return [], {}, []


def client_time():
    try:
        ev = events_mod()
        db = ev.connect()
        try:
            return ev.client_time(db, ev.load_config())
        finally:
            db.close()
    except Exception:
        return {"pending": [], "authorized": [], "unconfirmed": [], "recent": []}


WAIT_GRACE = 15 * 60
WAIT_MAX = 120 * 60
RESUMED_SLACK = 90


def watch_state(status, last_active, ev):
    """None for tabs that never declared a state; otherwise the declared state checked against reality."""
    if not ev:
        return None
    w = {"kind": ev["kind"], "text": ev["text"], "at": ev["at"], "until": ev["until"]}
    if status == "working":
        return {**w, "state": "working"}
    resumed = last_active and last_active > ev["at"] + RESUMED_SLACK
    if ev["kind"] == "ended":
        return None
    if ev["kind"] in ("done", "failed"):
        return None if resumed else {**w, "state": ev["kind"]}
    if resumed:
        return {**w, "state": "stopped-silent"}
    if ev["kind"] == "waiting":
        deadline = (ev["until"] + WAIT_GRACE) if ev["until"] else ev["at"] + WAIT_MAX
        return {**w, "state": "waiting" if time.time() < deadline else "overdue", "deadline": deadline}
    return {**w, "state": "stopped-silent"}


def age(ts):
    s = time.time() - ts
    if s < 3600:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // 3600)}h"
    return f"{int(s // 86400)}d"


STATE_FILES = ("graph.json", "decisions.md", "PENDING.md", "STATUS.md", "plan.md", "APPROVAL-QUEUE.md")


def latest_mtime(d):
    times = [d.stat().st_mtime]
    for name in STATE_FILES:
        try:
            times.append((d / name).stat().st_mtime)
        except OSError:
            pass
    return max(times)


def pending_items(path):
    items = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.startswith("- "):
            continue
        m = re.match(r"- \*\*(.+?)\*\*", line)
        items.append((m.group(1) if m else line[2:]).strip())
    return items


def epic_stage(d):
    """planning (a /plan-epic tab owns it), ready (handed off, no lead yet) or live; no PLANNING.json = live.
    adw-status refuses a lead registration while planning, so lead.json only appears after the handoff."""
    if not (d / "PLANNING.json").exists():
        return "live"
    if not (d / "READY.json").exists():
        return "planning"
    return "live" if (d / "lead.json").exists() else "ready"


def load_epic(d):
    try:
        g = json.loads((d / "graph.json").read_text())
    except (OSError, ValueError):
        if not (d / "lead.json").exists():
            return None
        g = {}
    segs = g.get("segments") or []
    by = {}
    for s in segs:
        by.setdefault(s.get("status", "?"), []).append(s)
    pj = d / "PENDING.md"
    pending = None
    if pj.exists():
        pending = {"items": pending_items(pj), "mtime": pj.stat().st_mtime}
    tokens = {f"epics/{d.name}", f"⧉ {d.name}"}
    for s in segs:
        if s.get("worktree"):
            tokens.add(s["worktree"])
    done = d / "COMPLETE.json"
    try:
        paused = json.loads((d / "PAUSED.json").read_text())
    except (OSError, ValueError):
        paused = None
    try:
        lead = json.loads((d / "lead.json").read_text())
    except (OSError, ValueError):
        lead = None
    return {
        "lead": lead,
        "lead_pane": None,
        "epic": d.name,
        "completed_at": json.loads(done.read_text()).get("completed_at") if done.exists() else None,
        "repo": g.get("repo"),
        "paused": paused,
        "stage": epic_stage(d),
        "max_parallel": g.get("max_parallel"),
        "mtime": latest_mtime(d),
        "counts": {k: len(v) for k, v in by.items()},
        "segments": {k: [{"id": s["id"], "pr": s.get("pr"), "note": s.get("note")} for s in v]
                     for k, v in by.items() if k in SEGMENT_STATUSES},
        "pending": pending,
        "tokens": sorted(tokens),
    }


BACKGROUND = re.compile(r"Waiting for \d+ background|^\s*◯ \S", re.M)
ASKS_YOU = re.compile(
    r"\?\s*$|\byour call\b|\byou need to\b|\byou(?:'ll)? (?:want|need) to (?:decide|pick|choose)\b|\bdecide\b"
    r"|\bwant me to\b|\bshould I\b|\bwould you (?:rather|like)\b|\blet me know\b|\bsay (?:so|which|the word)\b"
    r"|\bpick\b|\bapprove\b|\bif you (?:like|want)\b|\bwaiting (?:on|for) (?:you|your)\b", re.I | re.M)


last_turn = asks.mux.last_turn


def summary(text):
    lines = text.splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].startswith("※ recap:"):
            out = [lines[i][len("※ recap:"):].strip()]
            for l in lines[i + 1:]:
                if not l.startswith("  ") or not l.strip():
                    break
                out.append(l.strip())
            return re.sub(r"\s*\(disable recaps in /config\)", "", " ".join(out))[:500]
    turn, _ = last_turn(text)
    body = " ".join(l.strip() for l in turn.splitlines()
                    if l.strip() and not l.lstrip().startswith(("✻", "⎿", "─")))
    return body.lstrip("⏺ ")[:500]


def classify(status, text):
    """working | blocked | background | asks | unsent | finished | idle"""
    if status in ("working", "blocked", "unknown"):
        return status
    turn, draft = last_turn(text)
    if BACKGROUND.search(text):
        return "background"
    if draft:
        return "unsent"
    if ASKS_YOU.search(turn):
        return "asks"
    return "finished" if status == "done" else "idle"


def herdr(*args, machine=None):
    cmd, env = asks.herdr_cmd(args, machine)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    if r.returncode != 0:
        return None
    return r.stdout


def words(s):
    return {w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", s or "")}


def last_message_at(path):
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 262144))
        tail = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(tail):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("timestamp") and e.get("type") in ("user", "assistant", "response_item", "event_msg"):
            return datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")).timestamp()
    return None


IN_TAB_WORK = re.compile(r"← \d+ agent|\d+ background|Waiting for \d+ background")
REFRESH_AT = int(os.environ.get("ADW_LEAD_REFRESH_AT", "150000"))
REFRESH_LONG_WAIT_AT = int(os.environ.get("ADW_LEAD_REFRESH_LONG_WAIT_AT", "120000"))
LONG_WAIT = 3600


def refresh_at(w):
    """Past the 1h prompt-cache TTL a lead's resume re-writes its whole context, so long waits refresh smaller."""
    return REFRESH_LONG_WAIT_AT if w.get("until") and w["until"] - time.time() >= LONG_WAIT else REFRESH_AT


def context_tokens(sid):
    for path in (Path.home() / ".claude/projects").glob(f"*/{sid}.jsonl"):
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 524288))
            tail = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(tail):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            u = e.get("type") == "assistant" and (e.get("message") or {}).get("usage")
            if u:
                return u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    return None


_MATE_SIDS = {}


def is_mate_session(sid):
    if sid not in _MATE_SIDS:
        hit = False
        for path in (Path.home() / ".claude/projects").glob(f"*/{sid}.jsonl"):
            with open(path, "rb") as f:
                hit = b"<command-name>/first-mate</command-name>" in f.read(4194304)
            break
        _MATE_SIDS[sid] = hit
    return _MATE_SIDS[sid]


def last_active(agent, sid):
    if not sid:
        return None
    root = Path.home() / (".claude/projects" if agent == "claude" else ".codex/sessions")
    pattern = f"*/{sid}.jsonl" if agent == "claude" else f"**/*{sid}.jsonl"
    times = [t for t in (last_message_at(p) for p in root.glob(pattern)) if t]
    return max(times, default=None)


def load_agents(epics):
    raw, ok = [], False
    out = herdr("agent", "list") if shutil.which("herdr") else None
    if out is not None:
        ok = True
        raw = json.loads(out)["result"]["agents"]
        for m in asks.live_remotes():
            rout = herdr("agent", "list", machine=m)
            for a in json.loads(rout)["result"]["agents"] if rout else []:
                raw.append({**a, "pane_id": f"{m}/{a['pane_id']}", "machine": m})
    if asks.mux.available():
        hc_agents = terminals.agents(LIVE)
        if hc_agents is None:
            hc_agents = asks.mux.list_agents()
        if hc_agents is not None:
            ok = True
            raw += hc_agents
    if not ok:
        return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(lambda a: asks.read_pane(a["pane_id"], 60)[0] or "", raw))
    tab_labels = {}
    for m in [None, *asks.live_remotes()] if shutil.which("herdr") else []:
        tout = herdr("tab", "list", machine=m)
        for t in json.loads(tout)["result"]["tabs"] if tout else []:
            tab_labels[(m, t["tab_id"])] = t.get("label") or ""
    links = load_links()
    leads = {}
    for e in epics:
        if (e.get("lead") or {}).get("session"):
            leads.setdefault(e["lead"]["session"], []).append(e)
    agents = []
    for a, text in zip(raw, texts):
        sid = (a.get("agent_session") or {}).get("value")
        title = a.get("terminal_title_stripped") or ""
        twords = words(title)
        label = tab_labels.get((a.get("machine"), a.get("tab_id")), "")
        if label and not re.fullmatch(r"t?\d+", label):
            title = label
        linked = links.get(sid) if sid else None
        active = last_active(a["agent"], sid)
        led_all = leads.get(sid) if sid else None
        led = led_all[0] if led_all else None
        for e in led_all or []:
            e["lead_pane"] = a["pane_id"]
        epic = (led and led["epic"]) or linked or next((e["epic"] for e in epics
                               if words(e["epic"]) <= twords or any(t in text for t in e["tokens"])), None)
        agents.append({
            "pane": a["pane_id"],
            "kind": a["agent"],
            "status": a["agent_status"],
            "title": title,
            "terminal_title": a.get("terminal_title_stripped") or "",
            "cwd": a.get("cwd", "").replace(str(Path.home()), "~"),
            "machine": a.get("machine") or "",
            "epic": epic,
            "linked": bool(linked or led),
            "role": "lead" if led else "crew" if epic else "loose",
            "session": sid,
            "watch": watch_state(a["agent_status"], active, adw_status.last_event(sid)),
            "attention": classify(a["agent_status"], text),
            "summary": a["status_msg"] if a["agent_status"] == "blocked" and a.get("status_msg") else summary(text),
            "msg": a.get("status_msg") or "",
            "changed_at": a.get("changed_at"),
            "last_active": active,
            "unseen": a["agent_status"] == "done",
            "screen": "\n".join(text.rstrip().splitlines()[-25:]),
        })
    for a in agents:
        e = next((e for e in epics if e["epic"] == a["epic"]), None)
        a["lead_pane"] = e and e["lead_pane"]
        a["context"] = context_tokens(a["session"]) if a["role"] == "lead" and a["kind"] == "claude" else None
    return agents


def role_s(a):
    if a.get("role") == "lead":
        return f" [lead of {a['epic']}]"
    if a.get("role") == "crew":
        return f" [crew of {a['epic']}; " + (f"lead {a['lead_pane']}]" if a.get("lead_pane") else "no live lead]")
    return " [loose]"


def mate_digest():
    p = MATE / "digest.md"
    try:
        return {"text": p.read_text()[:20000], "at": p.stat().st_mtime}
    except OSError:
        return None


SLIM_KEYS = ("id", "status", "title", "epic", "pane", "created", "recommend", "default_at",
             "delivered", "undeliverable", "delivery_error")


def slim_decision(d):
    if d["status"] == "open":
        return d
    a = d.get("answer") or {}
    out = {k: d[k] for k in SLIM_KEYS if k in d}
    out["answer"] = {k: a[k] for k in ("choice", "by", "at") if k in a}
    out["options"] = [{"key": o["key"], "label": o.get("label")} for o in d.get("options") or [] if o.get("key") == a.get("choice")]
    out["slim"] = True
    return out


def slim_board(b):
    return {**b, "decisions": [slim_decision(d) for d in b.get("decisions") or [] if d["status"] != "withdrawn"]}


def build(args):
    cutoff = time.time() - args.days * 86400
    epics, completed = [], []
    for d in sorted(EPICS.iterdir() if EPICS.is_dir() else []):
        if not d.is_dir():
            continue
        e = load_epic(d)
        if e and e["completed_at"]:
            rep = d / "REPORT.json"
            try:
                r = json.loads(rep.read_text()) if rep.exists() else {}
            except ValueError:
                r = {}
            completed.append({"epic": e["epic"], "repo": e["repo"], "completed_at": e["completed_at"],
                              "report": {k: r.get(k) for k in ("active_days", "held_days", "prs_merged", "segments",
                                                               "cycle_h_median", "fp_rounds_median", "user_decisions",
                                                               "spend_usd", "usd_per_pr")} if r else None})
        elif e and (args.all or e["mtime"] >= cutoff):
            epics.append(e)
    epics.sort(key=lambda e: -e["mtime"])
    agents = None if args.no_herdr or not (shutil.which("herdr") or asks.mux.available()) else load_agents(epics)
    held = {e["epic"] for e in epics if e["paused"]}
    for a in agents or []:
        if a.get("epic") in held and a.get("watch") and a["watch"]["state"] in ("stopped-silent", "overdue", "waiting"):
            a["watch"]["state"] = "paused"
    since = time.time() - ANSWERED_SHOWN_HOURS * 3600
    decisions = [d for d in asks.all_asks()
                 if d["status"] == "open" or (d.get("answer") or {}).get("at", 0) >= since]
    interrupts, last_events, cleared = event_state()
    b = {"generated": int(time.time()), "epics": epics, "agents": agents, "decisions": decisions,
            "interrupts": interrupts, "cleared": cleared, "last_events": last_events, "client_time": client_time(),
            "ask_fresh_hours": ASK_FRESH_HOURS, "snoozes": load_snoozes(), "pins": load_pins(), "no_herdr": args.no_herdr,
            "completed": sorted(completed, key=lambda c: -c["completed_at"]),
            "mate": mate_digest(),
            "mate_pane": next((a["pane"] for a in agents or [] if a["kind"] == "claude" and a["session"]
                               and is_mate_session(a["session"])), None)}
    now = time.time()
    b["requests"] = [{**r, "stale": reqs.stale(r, now), "overdue": reqs.overdue(r)} for r in reqs.open_items()]
    b["needs"] = needs_you(b)
    return b


MATE_INTERRUPT_KINDS = ("ticket.authorize", "efficiency.due")
MATE_ADDRESSED = re.compile(r"\b(?:first[ -]?)?mate\b(?:\s*\([^)]*\))?\s*[:,]", re.I)


def needs_you(b):
    out = []
    for d in b.get("decisions") or []:
        if d["status"] == "open" and not snoozed(b, "d:" + d["id"]):
            out.append({"key": "d:" + d["id"], "kind": "decision", "id": d["id"], "title": d["title"], "age": d.get("created"),
                        "epic": d.get("epic"), "source": "decision"})
    for i in b.get("interrupts") or []:
        if i["kind"] not in MATE_INTERRUPT_KINDS:
            out.append({"key": f"i:{i['id']}", "kind": "interrupt", "id": i["id"], "title": i["title"], "age": i.get("created"),
                        "epic": i.get("epic"), "source": i["source"], "count": i.get("count", 1), "reopened_by": i.get("reopened_by"),
                        "reopened_at": i.get("reopened_at")})
    ct = b.get("client_time") or {}
    for t in ct.get("pending") or []:
        out.append({"key": "t:" + str(t["item_id"]), "kind": "authorize", "id": str(t["item_id"]),
                    "title": f"{t.get('client')}: {t.get('title')}", "age": t.get("requested_at"), "epic": None, "source": "ticket"})
    for r in ct.get("unconfirmed") or []:
        out.append({"key": "h:" + str(r.get("id")), "kind": "hours", "id": r.get("id"), "title": r.get("title"), "age": None,
                    "epic": None, "source": "client_time", "client": r.get("client"), "hours": r.get("hours")})
    for a in b.get("agents") or []:
        if a["attention"] == "blocked" and not snoozed(b, dialog_key(a)):
            out.append({"key": dialog_key(a), "kind": "dialog", "id": a["pane"], "title": a.get("msg") or a["title"], "age": a.get("last_active"),
                        "epic": a.get("epic"), "source": "agent", "pane": a["pane"]})
    return out


def seg_list(segs):
    return ", ".join(f"{s['id']}" + (f" #{s['pr']}" if s.get("pr") else "") for s in segs)


def render(b, color, full=False):
    B, D, R, Y, G, X = (("\033[1m", "\033[2m", "\033[31m", "\033[33m", "\033[32m", "\033[0m")
                        if color else ("",) * 6)
    epics, agents = b["epics"], b["agents"]
    lines = [f"{B}NEEDS YOU{X}"]
    n = 0
    for d in b.get("decisions", []):
        if d["status"] == "open":
            lines.append(f"  {d['id']:<8} {Y}decision{X}     {d['title']}" + (f"  {D}[{d['epic']}]{X}" if d.get("epic") else ""))
            n += 1
    for a in agents or []:
        if a["attention"] == "blocked":
            tag = {"blocked": f"{R}blocked{X}", "asks": f"{Y}asks you{X}", "unsent": f"{Y}unsent draft{X}"}[a["attention"]]
            lines.append(f"  {a['pane']:<8} {tag:<12} {a['title']}" + (f"  {D}[{a['epic']}]{X}" if a["epic"] else ""))
            n += 1
    if not n:
        lines.append(f"  {G}nothing{X}")
    if b.get("requests"):
        lines.append(f"\n{B}REQUESTS{X} {D}(req ls){X}")
        for r in b["requests"]:
            flags = "".join(f" {R}{f.upper()}{X}" for f in ("stale", "overdue") if r.get(f))
            lines.append(f"  {r['id']:<8} {r['priority']} {r['status']:<8} {plain(r['who'])}: {plain(r['title'])}" + flags)
    trouble = [a for a in agents or [] if a.get("watch") and a["watch"]["state"] in ("overdue", "stopped-silent", "failed")]
    waiting = [a for a in agents or [] if a.get("watch") and a["watch"]["state"] == "waiting"]
    if trouble or waiting:
        lines.append(f"\n{B}WATCH{X} {D}(states tabs declared with adw-status){X}")
        for a in trouble + waiting:
            w = a["watch"]
            c = R if w["state"] in ("failed", "stopped-silent") else Y if w["state"] == "overdue" else D
            lines.append(f"  {a['pane']:<8} {c}{w['state']:<14}{X} {a['title'][:40]:<40} {D}{w['text'][:70]}{X}")
    lines.append(f"\n{B}EPIC FILES{X} {D}(graph.json / PENDING.md; only as current as the conductor keeps them){X}")
    for e in epics:
        for st in NEEDS_YOU:
            for sg in e["segments"].get(st, []):
                lines.append(f"  {e['epic']:<30} {st}: {sg['id']}" + (f" #{sg['pr']}" if sg.get("pr") else "")
                             + (f"  {D}{(sg.get('note') or '')[:90]}{X}" if sg.get("note") else ""))
        p = e["pending"]
        if p and p["items"]:
            stale = time.time() - p["mtime"] > PENDING_STALE_DAYS * 86400
            hdr = f"  {e['epic']:<30} PENDING.md, {len(p['items'])} items, updated {age(p['mtime'])} ago"
            lines.append(hdr + (f" {Y}(stale?){X}" if stale else ""))
            shown = p["items"] if full else p["items"][-PENDING_SHOWN:]
            if len(shown) < len(p["items"]):
                lines.append(f"      {D}... {len(p['items']) - len(shown)} older (--full){X}")
            for it in shown:
                lines.append(f"      {D}-{X} {it[:110]}")

    lines.append(f"\n{B}EPICS{X} {D}(touched in window; segments in flight / max_parallel){X}")
    for e in epics:
        c = e["counts"]
        fl = sum(c.get(s, 0) for s in IN_FLIGHT)
        tabs = [a["pane"] for a in agents or [] if a["epic"] == e["epic"]]
        head = (f"  {e['epic']:<30} {e['repo'] or '?':<14} {fl}/{e['max_parallel'] or '?'}  "
                f"merged {c.get('merged', 0)}  ready {c.get('ready', 0)}  pending {c.get('pending', 0)}  "
                f"{D}{age(e['mtime'])} ago{X}" + (f"  tabs {','.join(tabs)}" if tabs else f"  {D}no tab{X}"))
        lines.append(head)
        for st in IN_FLIGHT:
            if e["segments"].get(st):
                lines.append(f"      {st}: {seg_list(e['segments'][st])}")

    if agents is not None:
        work = [a for a in agents if a["status"] == "working"]
        lines.append(f"\n{B}TABS{X} {D}{len(agents)} agents: {len(work)} working, "
                     f"{sum(a['status'] == 'idle' for a in agents)} idle{X}")
        for a in work:
            lines.append(f"  {a['pane']:<8} {G}working{X}  {a['kind']:<6} {a['title']}"
                         + (f"  {D}[{a['epic']}]{X}" if a["epic"] else ""))
    elif not b.get("no_herdr"):
        lines.append(f"\n{D}(herdr not available; tab state omitted){X}")
    return "\n".join(lines)


RECONCILE = (
    "[adw-board] the user asks: please reconcile epic {epic}. The board shows segments marked blocked or pr-ready in "
    "graph.json and items in PENDING.md that may already be resolved (merged PRs, decisions already made). "
    "Run adw-lead-state reconcile {epic}, bring graph.json statuses up to date (check each segment's PR and tab state), remove resolved "
    "items from PENDING.md, and file anything that still genuinely needs the user as an adw-ask decision with the "
    "full context. Reply with one line saying what changed."
)


PAUSE_MSG = ("[adw-board] The user put this epic ON HOLD (PAUSED.json). Dispatch nothing new. Let in-flight /adw conductors "
             "reach a checkpoint (PR pushed or work committed in its worktree), stop any waiting on CI or the review bot, "
             "update STATUS.md with where each segment stands, then `adw-status waiting \"epic paused\" --for 7d` and end the turn.")
RESUME_MSG = ("[adw-board] The user RESUMED this epic. Run `adw-lead-state reconcile <epic>`, re-read STATUS.md, and continue from the "
              "dispatch step.")
_ws_cache = {"at": 0, "data": []}


def workspaces():
    if time.time() - _ws_cache["at"] < 60:
        return _ws_cache["data"]
    out = herdr("workspace", "list")
    if out is None:
        return []
    data = []
    for w in json.loads(out)["result"]["workspaces"]:
        panes = herdr("pane", "list", "--workspace", w["workspace_id"])
        cwds = [p.get("cwd") for p in (json.loads(panes)["result"].get("panes", []) if panes else []) if p.get("cwd")]
        if not cwds:
            continue
        root = Path(min(cwds, key=lambda c: (c.count("/"), -cwds.count(c))))
        children = sorted((c for c in root.iterdir() if c.is_dir() and not c.name.startswith(".")),
                          key=lambda c: -c.stat().st_mtime)[:300] if root.is_dir() else []
        data.append({"id": w["workspace_id"], "label": w["label"], "dir": str(root),
                     "children": [c.name for c in children]})
    _ws_cache.update(at=time.time(), data=data)
    return data


def launch(body):
    ws = next((w for w in workspaces() if w["id"] == body.get("workspace")), None)
    if not ws:
        raise ValueError("unknown workspace")
    root = Path(ws["dir"]).resolve()
    target = (root / (body.get("subdir") or "").strip().strip("/")).resolve()
    if target != root and root not in target.parents:
        raise ValueError("folder must be inside the workspace")
    if not target.is_dir():
        raise ValueError(f"no such folder: {target}")
    label = (body.get("label") or "").strip()[:40] or target.name
    slug = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")[:24] or "agent"
    name = f"{slug if slug[0].isalpha() else 'a-' + slug}-{secrets.token_hex(2)}"[:32]
    out = herdr("tab", "create", "--workspace", ws["id"], "--cwd", str(target), "--label", label, "--no-focus")
    if out is None:
        raise ValueError("herdr could not create the tab")
    pane = json.loads(out)["result"]["root_pane"]["pane_id"]
    err = None
    for _ in range(4):
        r = subprocess.run(["herdr", "agent", "start", name, "--kind", "claude", "--pane", pane, "--timeout", "45000",
                            "--", *launch_args()], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            err = None
            break
        err = (r.stderr or r.stdout).strip()[:300]
        time.sleep(1.5)
    if err:
        return {"ok": False, "pane": pane, "error": f"tab created but claude did not start: {err}"}
    prompt = (body.get("prompt") or "").strip()
    if prompt:
        _, perr = asks.herdr("agent", "prompt", pane, prompt)
        if perr:
            return {"ok": True, "pane": pane, "name": name, "dir": str(target), "error": f"started, but the prompt failed: {perr}"}
    return {"ok": True, "pane": pane, "name": name, "dir": str(target)}


VIEW_KEYS = {"esc", "enter", "up", "down", "left", "right", "tab", "shift+tab", "ctrl+c",
             "1", "2", "3", "4", "5", "y", "n"}
PANE_ID = re.compile(r"(?:\w+/)?w\w+:p\w+|" + asks.mux.PANE_PATTERN)


def pane_view(pane, lines):
    if not PANE_ID.fullmatch(pane or ""):
        raise ValueError("bad pane id")
    n = str(max(20, min(lines, 400)))
    if asks.mux.is_hc(pane):
        out, _ = asks.mux.read(pane, int(n))
    else:
        out = herdr("pane", "read", pane, "--source", "recent", "--lines", n, "--format", "ansi")
        if out is not None and not out.strip():
            out = herdr("pane", "read", pane, "--source", "visible", "--lines", n, "--format", "ansi")
    if out is None:
        raise ValueError("cannot read that pane")
    info = asks.agent_info(pane)
    return {"pane": pane, "text": out, "status": info and info["status"]}


def serve(args):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    global LIVE
    if asks.mux.available():
        LIVE = terminals.start()
    token = secrets.token_urlsafe(24)
    page = (Path(__file__).resolve().parent / "board_page.html").read_text().replace("__ADW_TOKEN__", token).encode()
    allowed = {f"127.0.0.1:{args.serve}", f"localhost:{args.serve}"}
    origins = {f"http://{h}" for h in allowed}
    ts_host = os.environ.get("ADW_BOARD_TS_HOST", "").rstrip(".")
    ts_user = os.environ.get("ADW_BOARD_TS_USER", "")
    ts_hosts = {ts_host, f"{ts_host}:{args.serve}"} if ts_host and ts_user else set()
    origins |= {f"https://{h}" for h in ts_hosts}

    def permitted(headers):
        host = headers.get("Host")
        if host in allowed:
            return True
        # Requests proxied by `tailscale serve` carry the tailnet user's login; nothing else may use that host.
        return host in ts_hosts and headers.get("Tailscale-User-Login") == ts_user
    lock = threading.Lock()
    page_gz = gzip.compress(page, 9)
    cache = {"at": 0, "body": b"", "gz": b""}

    def board_json():
        with lock:
            if time.time() - cache["at"] > 5:
                asks.apply_defaults()
                asks.retry_deliveries()
                cache["body"] = json.dumps(slim_board(build(args))).encode()
                cache["gz"] = gzip.compress(cache["body"], 5)
                cache["at"] = time.time()
            return cache["body"], cache["gz"]

    class H(BaseHTTPRequestHandler):
        def send(self, code, ctype, body, gz=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Accept-Encoding")
            if accepts_gzip(self.headers.get("Accept-Encoding", "")):
                body = gz if gz is not None else gzip.compress(body, 5)
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # DNS-rebinding guard: only answer requests addressed to loopback by name or IP.
            if not permitted(self.headers):
                return self.send(403, "text/plain", b"forbidden host\n")
            path = self.path.split("?")[0]
            if path == "/":
                return self.send(200, "text/html; charset=utf-8", page, page_gz)
            if path == "/api/pane":
                from urllib.parse import parse_qs, urlsplit
                q = parse_qs(urlsplit(self.path).query)
                try:
                    v = pane_view(q.get("pane", [""])[0], int(q.get("lines", ["120"])[0]))
                except ValueError as e:
                    return self.send(400, "application/json", json.dumps({"error": str(e)}).encode())
                return self.send(200, "application/json", json.dumps(v).encode())
            if path == "/api/workspaces":
                return self.send(200, "application/json", json.dumps(workspaces()).encode())
            if path == "/api/board":
                return self.send(200, "application/json", *board_json())
            if path == "/api/decision":
                from urllib.parse import parse_qs, urlsplit
                try:
                    d = asks.load(parse_qs(urlsplit(self.path).query).get("id", [""])[0])
                except (SystemExit, ValueError) as e:
                    return self.send(404, "application/json", json.dumps({"error": str(e)}).encode())
                return self.send(200, "application/json", json.dumps(d).encode())
            if path == "/api/timelog.csv":
                from urllib.parse import parse_qs, urlsplit
                q = parse_qs(urlsplit(self.path).query)
                try:
                    days = [q.get(k, [""])[0] or None for k in ("from", "to")]
                    for d in days:
                        if d:
                            datetime.strptime(d, "%Y-%m-%d")
                    ev = events_mod()
                    db = ev.connect()
                    try:
                        buf = io.StringIO()
                        ev.timelog_csv(ev.timelog(db, *days), buf)
                    finally:
                        db.close()
                except ValueError as e:
                    return self.send(400, "application/json", json.dumps({"error": str(e)}).encode())
                return self.send(200, "text/csv; charset=utf-8", buf.getvalue().encode())
            if path in ("/api/feed", "/api/tldr"):
                from urllib.parse import parse_qs, urlsplit
                q = parse_qs(urlsplit(self.path).query)
                epic = q.get("epic", [""])[0]
                try:
                    after = int(q.get("after_id", ["0"])[0])
                    upto = int(q["upto"][0]) if q.get("upto") else None
                    if epic and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", epic):
                        raise ValueError("bad epic")
                    ev = events_mod()
                    db = ev.connect()
                    try:
                        res = (ev.feed(db, after, epic or None, order="asc" if q.get("order") == ["asc"] else "desc")
                               if path == "/api/feed" else ev.tldr(db, after, upto))
                    finally:
                        db.close()
                except (ValueError, OSError) as e:
                    return self.send(400, "application/json", json.dumps({"error": str(e)}).encode())
                return self.send(200, "application/json", json.dumps(res).encode())
            self.send(404, "text/plain", b"not found\n")

        def do_POST(self):
            # Answers type into agents running with bypassed permissions, so every POST must prove it came
            # from this page: loopback Host, same Origin, and the per-start token only the page holds.
            if (not permitted(self.headers)
                    or self.headers.get("Origin") not in origins
                    or self.headers.get("X-ADW-Token") != token
                    or not (self.headers.get("Content-Type") or "").startswith("application/json")):
                return self.send(403, "text/plain", b"forbidden\n")
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)))
                result = post(self.path, body)
            except (ValueError, KeyError, SystemExit) as e:
                return self.send(400, "application/json", json.dumps({"error": str(e)}).encode())
            with lock:
                cache["at"] = 0
            self.send(200, "application/json", json.dumps(result).encode())

        def log_message(self, *a):
            pass

    def post(route, body):
        if route == "/api/link":
            pane, epic = body.get("pane", ""), body.get("epic") or ""
            if epic and not (EPICS / epic / "graph.json").exists():
                raise ValueError("unknown epic")
            info = asks.agent_info(pane) if PANE_ID.fullmatch(pane) else None
            if not info or not info.get("session"):
                raise ValueError("no agent session in that pane")
            save_link(info["session"], epic)
            return {"ok": True}
        if route == "/api/keys":
            pane, keys = body.get("pane", ""), body.get("keys") or []
            if not PANE_ID.fullmatch(pane) or not keys or not all(k in VIEW_KEYS for k in keys):
                raise ValueError("bad pane or key")
            _, err = asks.send_keys(pane, keys)
            return {"ok": not err, "error": err}
        if route == "/api/launch":
            return launch(body)
        if route == "/api/epic":
            name = body.get("epic", "")
            d = EPICS / name
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) or not (d / "graph.json").exists():
                raise ValueError("unknown epic")
            marker = d / "COMPLETE.json"
            if body.get("action") == "complete":
                if body.get("confirm") != name:
                    raise ValueError("type the epic name exactly to confirm")
                marker.write_text(json.dumps({"completed_at": int(time.time()), "by": "adw-board"}) + "\n")
                report = Path(__file__).resolve().parent / "epic-report.py"
                if report.exists():
                    subprocess.Popen([sys.executable, str(report), name, "--write"],
                                     stdout=subprocess.DEVNULL, stderr=open(d / "REPORT.log", "w"), start_new_session=True)
            elif body.get("action") == "reopen":
                marker.unlink(missing_ok=True)
            elif body.get("action") in ("pause", "resume"):
                pz = d / "PAUSED.json"
                if body["action"] == "pause":
                    pz.write_text(json.dumps({"paused_at": int(time.time()), "by": "adw-board",
                                              "reason": (body.get("reason") or "").strip()[:300]}) + "\n")
                else:
                    pz.unlink(missing_ok=True)
                with open(d / "HOLDS.jsonl", "a") as f:
                    f.write(json.dumps({"event": body["action"], "at": int(time.time()), "by": "adw-board",
                                        "reason": (body.get("reason") or "").strip()[:300]}) + "\n")
                lead = next((a for a in json.loads(cache["body"] or b"{}").get("agents") or []
                             if a.get("epic") == name and a.get("role") == "lead"), None)
                if lead:
                    asks.prompt(lead["pane"], PAUSE_MSG if body["action"] == "pause" else RESUME_MSG)
                return {"ok": True, "lead_told": bool(lead)}
            else:
                raise ValueError("unknown action")
            return {"ok": True}
        if route == "/api/pin":
            set_pin(body.get("key", ""), bool(body.get("on")))
            return {"ok": True}
        if route == "/api/snooze":
            key = body.get("key", "")
            set_snooze(key, None if body.get("for") == "off" else snooze_until(body.get("for")))
            return {"ok": True}
        if route == "/api/interrupt-ack":
            ev = events_mod()
            db = ev.connect()
            try:
                return {"ok": ev.ack_interrupt(db, int(body["id"]), "owner")}
            finally:
                db.close()
        if route == "/api/interrupt-reopen":
            ev = events_mod()
            db = ev.connect()
            try:
                return {"ok": ev.reopen_interrupt(db, int(body["id"]), "owner")}
            finally:
                db.close()
        if route in ("/api/ticket-decision", "/api/work-confirm", "/api/work-log"):
            ev = events_mod()
            db = ev.connect()
            try:
                if route == "/api/ticket-decision":
                    return ev.decide_ticket(db, str(body.get("item_id", "")),
                                            {"authorize": "authorized", "authorized": "authorized", "deny": "denied",
                                             "denied": "denied"}.get(body.get("decision")))
                if route == "/api/work-confirm":
                    return {"ok": True, "hours": ev.confirm_work(db, body["id"], body.get("hours"))}
                ev.work_log_own(db, ev.load_config(), str(body.get("item_id", "")), body.get("hours"), body.get("note") or "")
                return {"ok": True}
            finally:
                db.close()
        if route == "/api/answer":
            d = asks.record_answer(body["id"], body.get("choice", ""), body.get("note", ""))
            return {"delivered": bool(d.get("delivered")), "delivery_error": d.get("delivery_error")}
        pane = body.get("pane", "")
        if not PANE_ID.fullmatch(pane):
            raise ValueError("bad pane id")
        if route == "/api/focus":
            _, err = asks.focus(pane)
            return {"ok": not err, "error": err}
        if route == "/api/reconcile":
            body["text"] = RECONCILE.format(epic=body.get("epic", "?"))
            route = "/api/reply"
        if route == "/api/reply":
            text = (body.get("text") or "").strip()
            if not text:
                raise ValueError("empty reply")
            info = asks.agent_info(pane)
            if not info:
                raise ValueError("no agent in that pane")
            if info["status"] == "blocked":
                raise ValueError("agent is in a dialog; open the tab to answer it")
            _, err = asks.prompt(pane, text)
            return {"ok": not err, "error": err}
        raise ValueError("unknown route")

    srv = ThreadingHTTPServer(("127.0.0.1", args.serve), H)
    print(f"adw-board on http://127.0.0.1:{args.serve}/", flush=True)
    srv.serve_forever()


MATE = ADW_HOME / "firstmate"
MATE_IGNORE_CWDS = [c.strip().rstrip("/") for c in os.environ.get("ADW_MATE_IGNORE_CWDS", "").split(",") if c.strip()]
DIGEST_TIMES = [t.strip() for t in os.environ.get("ADW_MATE_DIGEST_AT", "09:00,16:00").split(",") if t.strip()]


def for_mate(b):
    if b.get("agents") is not None:
        home = str(Path.home())
        ignored = [c.replace("~", home, 1) if c.startswith("~") else c for c in MATE_IGNORE_CWDS]
        b["agents"] = [a for a in b["agents"]
                       if not any(a["cwd"].replace("~", home, 1) == c or a["cwd"].replace("~", home, 1).startswith(c + "/")
                                  for c in ignored)
                       and not (a.get("session") and is_mate_session(a["session"]))]
    return b


DISK_ALERT_PCT = int(os.environ.get("ADW_DISK_ALERT_PCT", "80"))


def ago_s(ts):
    return age(ts) + " ago" if ts else "unknown"


def plain(text, cap=120):
    return " ".join(str(text).split())[:cap]


def untrusted(title, cap=120):
    return "untrusted title: " + json.dumps(" ".join(str(title).split())[:cap])


def interrupt_key(n):
    return f"interrupt:{n['id']}" + (f":r{int(n['reopened_at'])}" if n.get("reopened_at") else "")


def interrupt_line(n):
    flag = f" reopened by {n['reopened_by']}: do not clear" if n.get("reopened_by") else ""
    count = f" x{n['count']}" if n.get("count", 1) > 1 else ""
    return (f"needs-you interrupt {n['id']} [{n.get('epic') or '-'}] [{n['source']}]{flag}{count} "
            f"{untrusted(n['title'])}")


def mate_events(b):
    ev = {}
    agents = {a["pane"]: a for a in b.get("agents") or []}
    for n in needs_you(b):
        if n["kind"] == "decision":
            ev[f"decision:{n['id']}"] = f"needs-you decision {n['id']} [{n.get('epic') or '-'}] {n['title']}"
        elif n["kind"] == "interrupt":
            ev[interrupt_key(n)] = interrupt_line(n)
        elif n["kind"] == "dialog":
            a = agents[n["pane"]]
            ev[f"dialog:{a.get('session') or a['pane']}"] = f"needs-you dialog {a['pane']} {a['title']}{role_s(a)}"
    for r in b.get("requests") or []:
        if r.get("stale") or r.get("overdue"):
            why = "overdue" if r.get("overdue") else "stale"
            ev[f"request:{r['id']}:{why}:{int(r['updated'])}"] = (
                f"request {r['id']} {r['priority']} {why} ({r['status']}, owner {r.get('owner') or '-'}) "
                f"{plain(r['who'])}: {plain(r['title'])}")
    for i in b.get("interrupts") or []:
        if i["kind"] in MATE_INTERRUPT_KINDS:
            ev[interrupt_key(i)] = interrupt_line(i)
    for a in b.get("agents") or []:
        key = a.get("session") or a["pane"]
        w = a.get("watch")
        if w and w["state"] in ("stopped-silent", "overdue", "failed", "done"):
            kind = "ready" if w["state"] == "done" else "trouble"
            ev[f"watch:{key}:{w['state']}:{int(w['at'])}"] = f"{kind} {w['state']} {a['pane']} {a['title']}{role_s(a)} :: {w['text']}"
        elif w and w["state"] in ("waiting", "working") and a["pane"] != b.get("mate_pane") and (m := MATE_ADDRESSED.search(w["text"])):
            ask = w["text"][m.start():]
            ev[f"mate-request:{key}:{hashlib.sha1(ask.encode()).hexdigest()[:10]}"] = (
                f"mate-request {a['pane']} {a['title']}{role_s(a)} :: {ask}")
    for a in b.get("agents") or []:
        w = a.get("watch")
        if (a.get("role") == "lead" and (a.get("context") or 0) >= refresh_at(w or {}) and a["status"] in ("idle", "done")
                and w and w["state"] == "waiting" and "[refreshable]" in w["text"]
                and (a.get("last_active") or 0) <= w["at"] + 30 and a["attention"] != "background"
                and not IN_TAB_WORK.search(a.get("screen") or "")):
            ev[f"refresh:{a['session']}"] = (f"refresh {a['pane']} session {a['session']} [lead of {a['epic']}] at {a['context'] // 1000}k context, "
                                             f"waiting with nothing in flight in-tab :: {w['text']}")
    try:
        dk = json.loads((ADW_HOME / "disk.json").read_text())
        pct = dk.get("host_pct")
        if pct is not None and pct >= DISK_ALERT_PCT:
            ev[f"disk:host:{int(pct) // 5 * 5}"] = f"trouble disk host at {pct}% (alert at {DISK_ALERT_PCT}%)"
    except (OSError, ValueError):
        pass
    for e in b["epics"]:
        if e.get("stage") == "ready" and not e.get("paused"):
            ev[f"epic-ready:{e['epic']}"] = f"plan-ready epic {e['epic']} was approved in planning; start its lead"
        c = e["counts"]
        live_segs = c.get("building", 0) + c.get("pr-open", 0)
        open_segs = live_segs + sum(c.get(k, 0) for k in ("pr-ready", "ready", "pending", "blocked"))
        orphaned = (open_segs and not e.get("paused") and not e.get("completed_at") and e.get("stage", "live") == "live"
                    and b.get("agents") is not None)
        if orphaned and e.get("lead") and not e.get("lead_pane"):
            ev[f"leaderless:{e['epic']}:{e['lead']['session']}"] = (
                f"trouble leaderless {e['epic']}: lead {e['lead']['pane']} is gone with {live_segs} segment(s) in flight, "
                f"{open_segs - live_segs} waiting: adw-respawn {e['lead']['pane']}")
        elif orphaned and not e.get("lead"):
            ev[f"leaderless:{e['epic']}:none"] = (
                f"trouble leaderless {e['epic']}: no lead registered and {open_segs} open segment(s): the human starts a lead, "
                f"pauses or completes it")
        live = sum(v for k, v in c.items() if k in ("pending", "ready", "building", "pr-open", "blocked", "pr-ready"))
        if c.get("merged") and not live:
            ev[f"epic-done:{e['epic']}"] = f"ready epic {e['epic']} has every segment merged; mark complete?"
    return ev


def needs_text(items):
    lines = [f"Needs you ({len(items)})"]
    for n in items:
        count = f" x{n['count']}" if n.get("count", 1) > 1 else ""
        age_s = age(n["age"]) if n["age"] else "-"
        title = untrusted(n["title"]) if n["kind"] in ("interrupt", "authorize") else " ".join(str(n["title"]).split())[:120]
        lines.append(f"{n['key']}\t{n['kind']}\t[{n.get('epic') or '-'}] [{n['source']}] {title}{count}\t{age_s}")
    return "\n".join(lines) + "\n"


def digest(b):
    out = []
    needs, hrs = [], {}
    for n in needs_you(b):
        when = f" ({ago_s(n['age'])})" if n["age"] else ""
        if n["kind"] == "decision":
            needs.append(f"- decision `{n['id']}` [{n.get('epic') or '-'}] {n['title']}{when}")
        elif n["kind"] == "interrupt":
            needs.append(f"- interrupt `{n['id']}` [{n.get('epic') or '-'}] [{n['source']}] {untrusted(n['title'])}{when}")
        elif n["kind"] == "authorize":
            needs.append(f"- authorize ticket `{n['id']}` {untrusted(n['title'])}{when}")
        elif n["kind"] == "dialog":
            needs.append(f"- dialog `{n['pane']}` {n['title']} (active {ago_s(n['age'])})")
        else:
            h = hrs.setdefault(n["client"], [0, 0.0])
            h[0], h[1] = h[0] + 1, h[1] + n["hours"]
    n_snoozed = len(b.get("snoozes") or {})
    trouble = [f"- `{a['pane']}` {a['watch']['state']}: {a['title']}{role_s(a)} :: {a['watch']['text']} ({ago_s(a['watch']['at'])})"
               for a in b.get("agents") or [] if a.get("watch") and a["watch"]["state"] in ("failed", "stopped-silent", "overdue")]
    trouble += [f"- {line.removeprefix('trouble ')}" for k, line in mate_events(b).items() if k.startswith("leaderless:")]
    ready = [f"- `{a['pane']}` reported done: {a['watch']['text']}"
             for a in b.get("agents") or [] if a.get("watch") and a["watch"]["state"] == "done"]
    ready += [f"- {line.removeprefix('ready ')}" for k, line in mate_events(b).items() if k.startswith(("epic-done:", "epic-ready:"))]
    under = []
    for e in b["epics"]:
        segs = [s["id"] + (f" #{s['pr']}" if s.get("pr") else "") for st in ("building", "pr-open") for s in e["segments"].get(st, [])]
        if segs:
            under.append(f"- {e['epic']}: {len(segs)} in flight ({', '.join(segs[:6])}{'…' if len(segs) > 6 else ''})")
    under += [f"- `{a['pane']}` waiting: {a['watch']['text']}"
              for a in b.get("agents") or [] if a.get("watch") and a["watch"]["state"] == "waiting"]
    needs += [f"- client hours to confirm: {c} {h:.2f}h in {n} row(s)" for c, (n, h) in sorted(hrs.items())]
    requests = [f"- `{r['id']}` {r['priority']} {r['status']} · {plain(r['who'])}: {plain(r['title'])}"
                + "".join(f" · {f.upper()}" for f in ("stale", "overdue") if r.get(f))
                for r in b.get("requests") or []]
    auto = []
    for d in b.get("decisions") or []:
        by = (d.get("answer") or {}).get("by")
        if d["status"] == "answered" and by in ("auto", "mate") and d["answer"]["at"] > time.time() - 86400:
            label = next((o["label"] for o in d["options"] if o["key"] == d["answer"]["choice"]), "")
            who = " by mate" if by == "mate" else ""
            auto.append(f"- `{d['id']}` [{d.get('epic') or '-'}] {d['title']} -> ({d['answer']['choice']}) {label}{who} (pane {d.get('pane') or '-'}, {ago_s(d['answer']['at'])})")
    for title, items in (("Needs you", needs), ("Requests", requests), ("Trouble", trouble), ("Ready", ready), ("Under way", under), ("Auto-applied (24h)", auto)):
        out.append(f"## {title} ({len(items)})")
        out.extend(items or ["- nothing"])
        if title == "Needs you" and n_snoozed:
            out.append(f"- ({n_snoozed} snoozed on the board)")
        out.append("")
    rotations = rotate_module().recent()
    if rotations:
        out += [f"## Session compactions (24h) ({len(rotations)})", *rotations, ""]
    return "\n".join(out).rstrip() + "\n"


def show(b, target):
    if re.fullmatch(r"d-[0-9a-f]{6}", target):
        d = asks.load(target)
        opts = "\n".join(f"  ({o['key']}) {o['label']}" + (" [recommended]" if d.get("recommend") == o["key"] else "") for o in d["options"])
        return (f"decision {d['id']} [{d['status']}] epic={d.get('epic')} pane={d.get('pane')} filed {ago_s(d['created'])}\n"
                f"# {d['title']}\n\n{d['body']}\n\noptions:\n{opts or '  (free text)'}\n"
                + (f"answer: {json.dumps(d['answer'])}\n" if d.get("answer") else ""))
    a = next((a for a in b.get("agents") or [] if a["pane"] == target), None)
    if a:
        w = a.get("watch")
        return (f"tab {a['pane']} {a['kind']} [{a['status']}/{a['attention']}] {a['title']}\n"
                f"epic: {a.get('epic') or '-'}{' (linked)' if a.get('linked') else ''}{role_s(a)} · cwd {a['cwd']} · active {ago_s(a.get('last_active'))}\n"
                + (f"declared: {w['state']} — {w['kind']}: {w['text']}\n" if w else "")
                + f"summary: {a.get('summary') or '-'}\n\n--- last screen ---\n{a['screen']}\n")
    e = next((e for e in b["epics"] if e["epic"] == target), None)
    if e:
        segs = [f"  [{st}] {s['id']}" + (f" #{s['pr']}" if s.get("pr") else "") + (f" — {s['note']}" if s.get("note") else "")
                for st, ss in e["segments"].items() for s in ss]
        tabs = [f"{a['pane']} {a['title']}" for a in b.get("agents") or [] if a.get("epic") == e["epic"]]
        decs = [f"{d['id']} {d['title']}" for d in b.get("decisions", []) if d.get("epic") == e["epic"] and d["status"] == "open"]
        lead = (e["lead_pane"] or (f"{e['lead']['pane']} (gone)" if e.get("lead") else "none registered"))
        return (f"epic {e['epic']} ({e['repo']}) lead {lead} counts {json.dumps(e['counts'])} touched {ago_s(e['mtime'])}\n"
                + "\n".join(segs) + f"\ntabs: {', '.join(tabs) or '-'}\nopen decisions: {', '.join(decs) or '-'}\n")
    raise SystemExit(f"nothing on the board called {target!r} (try a decision id d-xxxxxx, a pane like w3:p4, or an epic slug)")


REFIRE_AFTER = 600


def watch(args):
    MATE.mkdir(parents=True, exist_ok=True)
    state_p = MATE / "watch-state.json"
    try:
        st = json.loads(state_p.read_text())
    except (OSError, ValueError):
        st = {}
    seen = set(st.get("seen", []))
    fired = st.get("fired", {})
    pending = set(st.get("pending", []))
    first = not st
    while True:
        try:
            asks.apply_defaults()
            b = for_mate(build(args))
            asks.retry_deliveries()
        except Exception as e:  # keep the stream alive across transient herdr/file errors
            print(f"watch-error {type(e).__name__}: {e}", flush=True)
            time.sleep(60)
            continue
        ev = mate_events(b)
        t = time.time()
        emitted = False
        if first:
            print(f"watch-started {len(ev)} current items (not replayed; run adw-board digest)", flush=True)
            first = False
            fired.update({k: t for k in ev})
        else:
            # An item fires once it has been present on two consecutive polls (a flickering dialog or a
            # state that clears within 30s never fires) and not within REFIRE_AFTER of its last firing.
            for k in sorted(set(ev) & pending):
                if t - fired.get(k, 0) > REFIRE_AFTER:
                    print(ev[k], flush=True)
                    fired[k] = t
                    emitted = True
            pending = set(ev) - seen
        seen = set(ev)
        fired = {k: v for k, v in fired.items() if t - v < 86400}
        st["fired"] = fired
        now = datetime.now()
        primed = "digests" in st
        st.setdefault("digests", [])
        for t in DIGEST_TIMES:
            slot = f"{now:%Y-%m-%d} {t}"
            if now.strftime("%H:%M") >= t and slot not in st["digests"]:
                if primed:
                    print(f"digest-due {t}", flush=True)
                    emitted = True
                st["digests"].append(slot)
        st["digests"] = st["digests"][-10:]
        st["seen"] = sorted(seen)
        st["pending"] = sorted(pending)
        state_p.write_text(json.dumps(st))
        if args.once and emitted:
            return
        time.sleep(30)


WATCHDOG_REFIRE = 2 * 3600
WATCHDOG_STALL_REFIRE = 4 * 3600
WATCHDOG_SILENT = 20 * 60
WATCHDOG_IDLE_BUILD = 60 * 60
WATCHDOG_GATED_ASK = 4 * 3600
DESK_TZ = ZoneInfo(os.environ.get("ADW_DESK_TZ", "UTC"))
DESK_HOURS = tuple(int(x) for x in os.environ.get("ADW_DESK_HOURS", "8-17").split("-"))
WATCHDOG_MORNING_HOUR = DESK_HOURS[0]
AWAY_HOLD = ("decision:", "ask-aging:", "watch:", "request:", "building-idle:", "mate-request:")


def at_desk(utc):
    local = utc.astimezone(DESK_TZ)
    return local.weekday() < 5 and DESK_HOURS[0] <= local.hour < DESK_HOURS[1]


def refire_held(k, utc):
    """Away from the desk, kinds whose keys never name a different event wait for the morning summary instead of refiring."""
    return k.startswith(AWAY_HOLD) and not at_desk(utc)


def stall_module():
    spec = importlib.util.spec_from_file_location("stall", Path(__file__).resolve().parent / "stall.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rotate_module():
    spec = importlib.util.spec_from_file_location("rotate", Path(__file__).resolve().parent / "rotate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rotate_sessions(full, dry_run=False):
    """Compacts idle leads and the first mate whose context is large; never fails the watchdog."""
    try:
        acts = rotate_module().run(full.get("agents") or [], mate_pane(full), asks.mux,
                                   lambda pane, text, guard=None, timeout=None: asks.mux.prompt(pane, text, timeout=timeout, guard=guard),
                                   dry_run=dry_run)
    except Exception as e:
        print(f"watchdog: rotation skipped: {e!r}", file=sys.stderr)
        return
    for a in acts:
        print(f"watchdog: rotate {a['event']} {a['pane']} {a.get('why') or ''}")


def lead_reachable(pane, session):
    info = asks.mux.get(pane)
    return bool(info) and info["status"] != "blocked" and (not session or info["session"] == session)


def lead_now(session, pane):
    """(current pane, status) of the lead found by its session, else its registered pane; None when it is gone."""
    found = asks.mux.terminal_for_session(session) if session else None
    where = asks.pane_ref(found) if found else pane
    info = asks.mux.get(where) if where else None
    if not info or (session and info["session"] != session):
        return None
    return where, info["status"]


def busy_panes(b):
    return {a.get("pane") for a in (b or {}).get("agents") or [] if a.get("attention") in ("unsent", "blocked", "asks")}


def stall_redispatch(b, mate, t, busy=None):
    busy = busy_panes(b) if busy is None else busy
    sessions = {}

    def occupied(pane):
        return pane in busy

    def resolve(session, pane):
        now = lead_now(session, pane)
        if now:
            sessions[now[0]] = session
        return (now[0], "blocked") if now and occupied(now[0]) else now

    def typed(pane, text, session):
        refused = []

        def guard():
            refused.append(asks.mux.idle_guard(pane, session)())
            return refused[-1]

        _, err = asks.prompt(pane, text, guard=guard)
        return None if refused and refused[-1] else err is None

    try:
        stall_module().redispatch(EPICS.parent, t, lambda pane, text: typed(pane, text, sessions.get(pane)), resolve,
                                  lambda text: bool(mate) and (asks.agent_info(mate) or {}).get("status") not in (None, "blocked")
                                  and not occupied(mate) and bool(typed(mate, text, None)), b)
    except Exception as e:
        print(f"watchdog: redispatch failed: {e!r}", file=sys.stderr)


def watchdog_items(b, t=None, write=False):
    t = t or time.time()
    ev = mate_events(b)
    quiet = {f"watch:{a.get('session') or a['pane']}:stopped-silent:{int(a['watch']['at'])}"
             for a in b.get("agents") or []
             if a.get("watch") and a["watch"]["state"] == "stopped-silent"
             and t - (a.get("last_active") or a["watch"]["at"]) <= WATCHDOG_SILENT}
    ev = {k: v for k, v in ev.items() if k not in quiet}
    agents = b.get("agents") or []
    for e in b["epics"]:
        if e.get("paused") or not e["segments"].get("building"):
            continue
        mine = [a for a in agents if a.get("epic") == e["epic"] or (e.get("lead_pane") and a["pane"] == e["lead_pane"])]
        if not mine or any(a["status"] in ("working", "unknown") or (a.get("watch") or {}).get("state") == "waiting" for a in mine):
            continue
        last = max([e["mtime"], *(a.get("last_active") or 0 for a in mine), *(a.get("changed_at") or 0 for a in mine)])
        if t - last > WATCHDOG_IDLE_BUILD:
            for s in e["segments"]["building"]:
                ev[f"building-idle:{e['epic']}:{s['id']}"] = (
                    f"trouble segment {s['id']} of {e['epic']} is building but the epic has had no activity for {age(last)}")
    try:
        ev.update(stall_module().items(b, t, EPICS.parent, write))
    except Exception as e:
        print(f"watchdog: stall skipped: {e!r}", file=sys.stderr)
    for d in b.get("decisions") or []:
        if d["status"] == "open" and t - d["created"] > WATCHDOG_GATED_ASK and (d.get("class") in asks.GATED or asks.gate_hit(d)):
            ev.pop(f"decision:{d['id']}", None)
            ev[f"ask-aging:{d['id']}"] = f"trouble gated decision {d['id']} [{d.get('epic') or '-'}] has waited {age(d['created'])}: {d['title']}"
    return ev


def refire_after(k):
    return WATCHDOG_STALL_REFIRE if k.startswith("stall:") else WATCHDOG_REFIRE


def watchdog_plan(items, st, t, utc):
    """Returns (header, lines, new_state); header is None when nothing should be sent."""
    fired = {k: v for k, v in (st.get("fired") or {}).items() if k in items or t - v <= refire_after(k)}
    new = [k for k in items if k not in fired or (t - fired[k] > refire_after(k) and not refire_held(k, utc))]
    local = utc.astimezone(DESK_TZ)
    today = f"{local:%Y-%m-%d}"
    morning = local.hour >= WATCHDOG_MORNING_HOUR and st.get("morning") != today
    ns = {"fired": fired, "morning": today if morning else st.get("morning")}
    if morning:
        sent = list(items)
        header = f"[watchdog] morning summary, {len(sent)} open:" if sent else "[watchdog] morning summary: nothing open"
    elif new:
        sent = new
        header = f"[watchdog] {len(sent)} items:"
    else:
        return None, [], ns
    for k in sent:
        fired[k] = t
    return header, [items[k] for k in sent], ns


def mate_pane(b):
    if b.get("mate_pane"):
        return b["mate_pane"]
    for a in asks.list_agents() or []:
        if a.get("name") == "first-mate":
            return a["pane_id"]
    return os.environ.get("ADW_MATE_PANE")


def stall_fresh(items, prior, t):
    return [k for k in items if k.startswith("stall:") and t - (prior.get("fired") or {}).get(k, -1e18) > refire_after(k)]


def stall_annotate(items, prior, t):
    """Adds the unreachable-lead note to fresh stall items; returns (items, {key: lead pane or None})."""
    keys = stall_fresh(items, prior, t)
    if not keys:
        return items, {}
    try:
        return stall_module().annotate(items, keys, EPICS.parent, lead_reachable)
    except Exception as e:
        print(f"watchdog: stall lead check failed: {e!r}", file=sys.stderr)
        return items, {k: None for k in keys}


def stall_deliver(items, targets, mate_ok, t):
    """Sends the fresh stall items to their epic leads and logs each fire; a stall failure never blocks the watchdog."""
    if not targets:
        return
    try:
        stall_module().deliver(list(targets), items, mate_ok, lambda pane, text: asks.prompt(pane, text)[1] is None,
                               EPICS.parent, t, targets)
    except Exception as e:
        print(f"watchdog: stall delivery failed: {e!r}", file=sys.stderr)


ENDED_AFTER = 30 * 60
ENDED_SCAN = 7 * 86400


def mark_ended(b, t=None, status_dir=None):
    """Closes the log of every session with no tab left whose last line is an open waiting/progress, so it never reads as overdue."""
    t = t or time.time()
    status_dir = Path(status_dir or adw_status.STATUS)
    live = {a.get("session") for a in b.get("agents") or [] if a.get("session")}
    stamp = datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    for p in status_dir.glob("*.log"):
        try:
            if p.stem in live or t - p.stat().st_mtime > ENDED_SCAN:
                continue
            ev = adw_status.last_event(p.stem, status_dir)
        except OSError:
            continue
        if not ev or ev["kind"] not in ("waiting", "progress") or t - ev["at"] < ENDED_AFTER:
            continue
        with open(p, "a") as f:
            f.write(f"{stamp} ended: tab gone; last said {ev['kind']}: {ev['text'][:120]}\n")
        n += 1
    return n


def watchdog(args):
    MATE.mkdir(parents=True, exist_ok=True)
    with open(EPICS.parent / "watchdog.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("watchdog: another run holds the lock")
            return
        watchdog_locked(args)


def watchdog_locked(args):
    if not args.dry_run:
        asks.apply_defaults()
        asks.retry_deliveries()
    full = build(args)
    if full.get("agents") is None:
        print("watchdog: no terminal backend available; skipping this run", file=sys.stderr)
        return
    if not args.dry_run:
        mark_ended(full)
    busy = busy_panes(full)
    b = for_mate(full)
    items = watchdog_items(b, write=not args.dry_run)
    state_p = MATE / "watchdog-state.json"
    try:
        st = json.loads(state_p.read_text())
    except (OSError, ValueError):
        st = {}
    if not isinstance(st, dict) or not isinstance(st.get("fired"), (dict, type(None))):
        st = {}
    t = time.time()
    items, targets = stall_annotate(items, st, t)
    header, lines, ns = watchdog_plan(items, st, t, datetime.fromtimestamp(t, timezone.utc))
    if header:
        text = "\n".join([header, *lines])
        if args.dry_run:
            print(text)
        else:
            mate_ok = False
            pane = mate_pane(b)
            info = asks.agent_info(pane) if pane else None
            if not info:
                print("watchdog: first-mate pane not found; will retry", file=sys.stderr)
            elif info["status"] == "blocked":
                print("watchdog: first mate is in a dialog; will retry", file=sys.stderr)
            else:
                _, err = asks.prompt(pane, text)
                if err:
                    print(f"watchdog: prompt failed: {err}; will retry", file=sys.stderr)
                else:
                    mate_ok = True
                    state_p.write_text(json.dumps(ns))
            stall_deliver(items, targets, mate_ok, t)
    if not args.dry_run:
        stall_redispatch(b, mate_pane(b), t, busy)
        tmp = MATE / f".digest.md.{os.getpid()}"
        tmp.write_text(digest(b))
        tmp.replace(MATE / "digest.md")
        if not header:
            state_p.write_text(json.dumps(ns))
    else:
        print(f"watchdog: {len(items)} items open, {len(lines)} would be sent")
    rotate_sessions(full, args.dry_run)


def main():
    ap = argparse.ArgumentParser(prog="adw-board")
    ap.add_argument("cmd", nargs="?", choices=("watch", "digest", "show", "needs", "watchdog"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("target", nargs="?")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--no-herdr", action="store_true")
    ap.add_argument("--once", action="store_true", help="watch: exit after the first poll that emits events")
    ap.add_argument("--serve", nargs="?", type=int, const=4518, metavar="PORT")
    args = ap.parse_args()
    if args.serve is not None:
        serve(args)
        return
    if args.cmd == "watch":
        watch(args)
        return
    if args.cmd == "watchdog":
        watchdog(args)
        return
    b = build(args)
    if args.cmd in ("digest", "show", "needs"):
        for_mate(b)
    if args.cmd == "digest":
        print(digest(b), end="")
        return
    if args.cmd == "needs":
        print(needs_text(needs_you(b)), end="")
        return
    if args.cmd == "show":
        if not args.target:
            raise SystemExit("usage: adw-board show <decision id | pane | epic>")
        print(show(b, args.target), end="")
        return
    if args.json:
        json.dump(b, sys.stdout, indent=2)
        print()
    else:
        print(render(b, sys.stdout.isatty(), args.full))


if __name__ == "__main__":
    main()
