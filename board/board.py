#!/usr/bin/env python3
"""adw-board: one view of what every epic and agent tab needs from you.

Usage: adw-board [--days N] [--all] [--full] [--json] [--no-herdr]
       adw-board --serve [PORT]                      web board on 127.0.0.1 (default 4518)
       adw-board digest | show <decision id | pane | epic> | watch

Env: ADW_HOME, ADW_LAUNCH_ARGS (args for agents launched from the board),
     ADW_BOARD_TS_HOST + ADW_BOARD_TS_USER (optional tailscale serve access), ADW_MATE_DIGEST_AT
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import importlib.util
import secrets
import shlex
import shutil
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ADW_HOME = Path(os.environ.get("ADW_HOME") or Path.home() / ".claude/adw")
EPICS = ADW_HOME / "epics"
IN_FLIGHT = ("building", "pr-open")
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

_spec = importlib.util.spec_from_file_location("asks", Path(__file__).resolve().parent / "asks.py")
asks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(asks)
_spec = importlib.util.spec_from_file_location("adw_status", Path(__file__).resolve().parent / "status.py")
adw_status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adw_status)

LAUNCH_ARGS = shlex.split(os.environ.get("ADW_LAUNCH_ARGS", "--dangerously-skip-permissions"))
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


def load_epic(d):
    try:
        g = json.loads((d / "graph.json").read_text())
    except (OSError, ValueError):
        return None
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
        lead = json.loads((d / "lead.json").read_text())
    except (OSError, ValueError):
        lead = None
    return {
        "lead": lead,
        "lead_pane": None,
        "epic": d.name,
        "completed_at": json.loads(done.read_text()).get("completed_at") if done.exists() else None,
        "repo": g.get("repo"),
        "max_parallel": g.get("max_parallel"),
        "mtime": latest_mtime(d),
        "counts": {k: len(v) for k, v in by.items()},
        "segments": {k: [{"id": s["id"], "pr": s.get("pr"), "note": s.get("note")} for s in v]
                     for k, v in by.items() if k in IN_FLIGHT + NEEDS_YOU + ("ready",)},
        "pending": pending,
        "tokens": sorted(tokens),
    }


BACKGROUND = re.compile(r"Waiting for \d+ background|^\s*◯ \S", re.M)
ASKS_YOU = re.compile(
    r"\?\s*$|\byour call\b|\byou need to\b|\byou(?:'ll)? (?:want|need) to (?:decide|pick|choose)\b|\bdecide\b"
    r"|\bwant me to\b|\bshould I\b|\bwould you (?:rather|like)\b|\blet me know\b|\bsay (?:so|which|the word)\b"
    r"|\bpick\b|\bapprove\b|\bif you (?:like|want)\b|\bwaiting (?:on|for) (?:you|your)\b", re.I | re.M)


def last_turn(text):
    lines = text.splitlines()
    prompt = max((i for i, l in enumerate(lines) if l.startswith("❯")), default=len(lines))
    start = max((i for i, l in enumerate(lines[:prompt]) if l.startswith("⏺ ")), default=0)
    return "\n".join(lines[start:prompt]), (lines[prompt][1:].strip() if prompt < len(lines) else "")


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
    if status in ("working", "blocked"):
        return status
    turn, draft = last_turn(text)
    if BACKGROUND.search(text):
        return "background"
    if draft:
        return "unsent"
    if ASKS_YOU.search(turn):
        return "asks"
    return "finished" if status == "done" else "idle"


def herdr(*args):
    r = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=15)
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


def last_active(agent, sid):
    if not sid:
        return None
    root = Path.home() / (".claude/projects" if agent == "claude" else ".codex/sessions")
    pattern = f"*/{sid}.jsonl" if agent == "claude" else f"**/*{sid}.jsonl"
    times = [t for t in (last_message_at(p) for p in root.glob(pattern)) if t]
    return max(times, default=None)


def load_agents(epics):
    out = herdr("agent", "list")
    if out is None:
        return None
    raw = json.loads(out)["result"]["agents"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        texts = list(pool.map(lambda a: herdr("agent", "read", a["pane_id"], "--source", "recent-unwrapped", "--lines", "60") or "", raw))
    links = load_links()
    leads = {e["lead"]["session"]: e for e in epics if (e.get("lead") or {}).get("session")}
    agents = []
    for a, text in zip(raw, texts):
        sid = (a.get("agent_session") or {}).get("value")
        title = a.get("terminal_title_stripped") or ""
        twords = words(title)
        linked = links.get(sid) if sid else None
        active = last_active(a["agent"], sid)
        led = leads.get(sid) if sid else None
        if led:
            led["lead_pane"] = a["pane_id"]
        epic = (led and led["epic"]) or linked or next((e["epic"] for e in epics
                               if words(e["epic"]) <= twords or any(t in text for t in e["tokens"])), None)
        agents.append({
            "pane": a["pane_id"],
            "kind": a["agent"],
            "status": a["agent_status"],
            "title": a.get("terminal_title_stripped") or "",
            "cwd": a.get("cwd", "").replace(str(Path.home()), "~"),
            "epic": epic,
            "linked": bool(linked or led),
            "role": "lead" if led else "crew" if epic else "loose",
            "session": sid,
            "watch": watch_state(a["agent_status"], active, adw_status.last_event(sid)),
            "attention": classify(a["agent_status"], text),
            "summary": summary(text),
            "last_active": active,
            "unseen": a["agent_status"] == "done",
            "screen": "\n".join(text.rstrip().splitlines()[-25:]),
        })
    for a in agents:
        e = next((e for e in epics if e["epic"] == a["epic"]), None)
        a["lead_pane"] = e and e["lead_pane"]
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


def build(args):
    cutoff = time.time() - args.days * 86400
    epics, completed = [], []
    for d in sorted(EPICS.iterdir() if EPICS.is_dir() else []):
        if not d.is_dir():
            continue
        e = load_epic(d)
        if e and e["completed_at"]:
            completed.append({"epic": e["epic"], "repo": e["repo"], "completed_at": e["completed_at"]})
        elif e and (args.all or e["mtime"] >= cutoff):
            epics.append(e)
    epics.sort(key=lambda e: -e["mtime"])
    agents = None if args.no_herdr or not shutil.which("herdr") else load_agents(epics)
    since = time.time() - ANSWERED_SHOWN_HOURS * 3600
    decisions = [d for d in asks.all_asks()
                 if d["status"] == "open" or (d.get("answer") or {}).get("at", 0) >= since]
    return {"generated": int(time.time()), "epics": epics, "agents": agents, "decisions": decisions,
            "ask_fresh_hours": ASK_FRESH_HOURS, "no_herdr": args.no_herdr,
            "completed": sorted(completed, key=lambda c: -c["completed_at"]),
            "mate": mate_digest()}


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
                            "--", *LAUNCH_ARGS], capture_output=True, text=True, timeout=60)
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
PANE_ID = re.compile(r"w\w+:p\w+")


def pane_view(pane, lines):
    if not PANE_ID.fullmatch(pane or ""):
        raise ValueError("bad pane id")
    n = str(max(20, min(lines, 400)))
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
    cache = {"at": 0, "body": b""}

    def board_json():
        with lock:
            if time.time() - cache["at"] > 5:
                asks.retry_deliveries()
                cache["body"] = json.dumps(build(args)).encode()
                cache["at"] = time.time()
            return cache["body"]

    class H(BaseHTTPRequestHandler):
        def send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # DNS-rebinding guard: only answer requests addressed to loopback by name or IP.
            if not permitted(self.headers):
                return self.send(403, "text/plain", b"forbidden host\n")
            path = self.path.split("?")[0]
            if path == "/":
                return self.send(200, "text/html; charset=utf-8", page)
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
                return self.send(200, "application/json", board_json())
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
            _, err = asks.herdr("pane", "send-keys", pane, *keys)
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
            elif body.get("action") == "reopen":
                marker.unlink(missing_ok=True)
            else:
                raise ValueError("unknown action")
            return {"ok": True}
        if route == "/api/answer":
            d = asks.record_answer(body["id"], body.get("choice", ""), body.get("note", ""))
            return {"delivered": bool(d.get("delivered")), "delivery_error": d.get("delivery_error")}
        pane = body.get("pane", "")
        if not re.fullmatch(r"w\w+:p\w+", pane):
            raise ValueError("bad pane id")
        if route == "/api/focus":
            _, err = asks.herdr("agent", "focus", pane)
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
            _, err = asks.herdr("agent", "prompt", pane, text)
            return {"ok": not err, "error": err}
        raise ValueError("unknown route")

    srv = ThreadingHTTPServer(("127.0.0.1", args.serve), H)
    print(f"adw-board on http://127.0.0.1:{args.serve}/", flush=True)
    srv.serve_forever()


MATE = ADW_HOME / "firstmate"
DIGEST_TIMES = [t.strip() for t in os.environ.get("ADW_MATE_DIGEST_AT", "09:00,16:00").split(",") if t.strip()]


def ago_s(ts):
    return age(ts) + " ago" if ts else "unknown"


def mate_events(b):
    ev = {}
    for d in b.get("decisions", []):
        if d["status"] == "open":
            ev[f"decision:{d['id']}"] = f"needs-you decision {d['id']} [{d.get('epic') or '-'}] {d['title']}"
    for a in b.get("agents") or []:
        key = a.get("session") or a["pane"]
        if a["attention"] == "blocked":
            ev[f"dialog:{key}"] = f"needs-you dialog {a['pane']} {a['title']}{role_s(a)}"
        w = a.get("watch")
        if w and w["state"] in ("stopped-silent", "overdue", "failed", "done"):
            kind = "ready" if w["state"] == "done" else "trouble"
            ev[f"watch:{key}:{w['state']}:{int(w['at'])}"] = f"{kind} {w['state']} {a['pane']} {a['title']}{role_s(a)} :: {w['text']}"
    for e in b["epics"]:
        live_segs = e["counts"].get("building", 0) + e["counts"].get("pr-open", 0)
        if e.get("lead") and not e.get("lead_pane") and live_segs and b.get("agents") is not None:
            ev[f"leaderless:{e['epic']}:{e['lead']['session']}"] = (
                f"trouble leaderless {e['epic']}: lead {e['lead']['pane']} is gone with {live_segs} segment(s) in flight")
        c = e["counts"]
        live = sum(v for k, v in c.items() if k in ("pending", "ready", "building", "pr-open", "blocked", "pr-ready"))
        if c.get("merged") and not live:
            ev[f"epic-done:{e['epic']}"] = f"ready epic {e['epic']} has every segment merged; mark complete?"
    return ev


def digest(b):
    out = []
    needs = [f"- decision `{d['id']}` [{d.get('epic') or '-'}] {d['title']} ({ago_s(d['created'])})"
             for d in b.get("decisions", []) if d["status"] == "open"]
    needs += [f"- dialog `{a['pane']}` {a['title']} (active {ago_s(a.get('last_active'))})"
              for a in b.get("agents") or [] if a["attention"] == "blocked"]
    trouble = [f"- `{a['pane']}` {a['watch']['state']}: {a['title']}{role_s(a)} :: {a['watch']['text']} ({ago_s(a['watch']['at'])})"
               for a in b.get("agents") or [] if a.get("watch") and a["watch"]["state"] in ("failed", "stopped-silent", "overdue")]
    trouble += [f"- {line.removeprefix('trouble ')}" for k, line in mate_events(b).items() if k.startswith("leaderless:")]
    ready = [f"- `{a['pane']}` reported done: {a['watch']['text']}"
             for a in b.get("agents") or [] if a.get("watch") and a["watch"]["state"] == "done"]
    ready += [f"- {line.removeprefix('ready ')}" for k, line in mate_events(b).items() if k.startswith("epic-done:")]
    under = []
    for e in b["epics"]:
        segs = [s["id"] + (f" #{s['pr']}" if s.get("pr") else "") for st in ("building", "pr-open") for s in e["segments"].get(st, [])]
        if segs:
            under.append(f"- {e['epic']}: {len(segs)} in flight ({', '.join(segs[:6])}{'…' if len(segs) > 6 else ''})")
    under += [f"- `{a['pane']}` waiting: {a['watch']['text']}"
              for a in b.get("agents") or [] if a.get("watch") and a["watch"]["state"] == "waiting"]
    for title, items in (("Needs you", needs), ("Trouble", trouble), ("Ready", ready), ("Under way", under)):
        out.append(f"## {title} ({len(items)})")
        out.extend(items or ["- nothing"])
        out.append("")
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
    pending = set()
    first = not st
    while True:
        try:
            b = build(args)
            asks.retry_deliveries()
        except Exception as e:  # keep the stream alive across transient herdr/file errors
            print(f"watch-error {type(e).__name__}: {e}", flush=True)
            time.sleep(60)
            continue
        ev = mate_events(b)
        t = time.time()
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
                st["digests"].append(slot)
        st["digests"] = st["digests"][-10:]
        st["seen"] = sorted(seen)
        state_p.write_text(json.dumps(st))
        time.sleep(30)


def main():
    ap = argparse.ArgumentParser(prog="adw-board")
    ap.add_argument("cmd", nargs="?", choices=("watch", "digest", "show"))
    ap.add_argument("target", nargs="?")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--no-herdr", action="store_true")
    ap.add_argument("--serve", nargs="?", type=int, const=4518, metavar="PORT")
    args = ap.parse_args()
    if args.serve is not None:
        serve(args)
        return
    if args.cmd == "watch":
        watch(args)
        return
    b = build(args)
    if args.cmd == "digest":
        print(digest(b), end="")
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
