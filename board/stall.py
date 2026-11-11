#!/usr/bin/env python3
"""adw-stall: deterministic epic stall rules feeding the board's [watchdog] items.

Usage: stall.py check --now [--root DIR]    dry run: print what would fire, write nothing
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ADW_STALL_ROOT") or os.environ.get("ADW_HOME") or Path.home() / ".claude/adw")
H = 3600
DEFAULTS = {
    "ready_idle": 2 * H,
    "answered": 1 * H,
    "frozen": 48 * H,
    "finish_pct": 0.70,
    "finish_idle": 3 * 86400,
    "refire": 4 * H,
    "promise_grace": 15 * 60,
    "promise_max_age": 24 * H,
    "answered_window": 7 * 86400,
    "redispatch": True,
    "redispatch_after": 1 * H,
    "redispatch_gap": 2 * H,
    "redispatch_max": 2,
    "redispatch_per_run": 3,
    "redispatch_notify_per_run": 1,
    "snooze": {},
}
HOLD_WORDS = re.compile(r"\b(held|parked|deferred)\b", re.I)
DONE = ("merged", "done", "deployed")
GONE = ("dropped", "superseded", "folded")
TERMINAL = DONE + GONE
HELD = ("held", "parked")
IN_FLIGHT = ("building", "pr-open", "in_progress", "pr-ready", "review")
IDLE_STATUSES = ("pending", "ready", "queued")
LIVE = IN_FLIGHT + IDLE_STATUSES + ("blocked",)
ASK_ID = re.compile(r"\bd-[0-9a-f]{6}\b")
PROMISE = re.compile(r"\b(?:at|by)\s+(\d{1,2}):(\d{2})\s*Z", re.I)
LINE = re.compile(r"^(\S+) (\w+): (.*?)(?: until (\S+))?$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS stall_snapshots(epic TEXT, seg TEXT, status TEXT, since REAL, PRIMARY KEY(epic, seg));
CREATE TABLE IF NOT EXISTS stall_fires(key TEXT, rule TEXT, epic TEXT, seg TEXT, ts REAL, text TEXT, delivered_to TEXT);
CREATE INDEX IF NOT EXISTS stall_fires_key ON stall_fires(key, ts);
CREATE TABLE IF NOT EXISTS redispatch_log(ask TEXT, epic TEXT, seg TEXT, ts REAL, target TEXT, outcome TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS redispatch_log_ask ON redispatch_log(ask, ts);
"""


def config(root):
    cfg = dict(DEFAULTS)
    try:
        cfg.update({k: v for k, v in json.loads((root / "stall.json").read_text()).items() if k in DEFAULTS})
    except (OSError, ValueError, AttributeError):
        pass
    return cfg


def refire(root=None):
    return config(root or ROOT)["refire"]


def span(s):
    if s < H:
        return f"{int(s // 60)}m"
    if s < 86400:
        return f"{int(s // H)}h"
    return f"{int(s // 86400)}d"


def iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def read_json(p):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def load_asks(root):
    out = {}
    for p in (root / "asks").glob("d-*.json"):
        d = read_json(p)
        if isinstance(d, dict) and d.get("id"):
            out[d["id"]] = d
    return out


def ask_state(a):
    if not a:
        return "missing"
    return a.get("status") or "open"


def ask_resolved(a):
    return ask_state(a) in ("answered", "withdrawn")


def load_epics(root):
    out = []
    for d in sorted((root / "epics").glob("*/")):
        g = read_json(d / "graph.json")
        if not isinstance(g, dict) or (d / "COMPLETE.json").exists() or (d / "PAUSED.json").exists():
            continue
        segs = [s for s in g.get("segments") or [] if isinstance(s, dict) and s.get("id")]
        if not segs:
            continue
        lead = read_json(d / "lead.json")
        try:
            mtime = (d / "graph.json").stat().st_mtime
        except OSError:
            mtime = 0
        out.append({"epic": d.name, "dir": d, "segments": segs, "max_parallel": g.get("max_parallel") or 0,
                    "lead": lead if isinstance(lead, dict) else {}, "mtime": mtime})
    return out


def events_ro(root):
    p = root / "events.db"
    if not p.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        return db
    except sqlite3.Error:
        return None


def history(db):
    """({(epic, seg, kind): latest ts}, {epic: latest merge ts})."""
    seg, merged = {}, {}
    if db is None:
        return seg, merged
    try:
        rows = db.execute("SELECT epic, object_ref, kind, ts FROM events WHERE kind IN "
                          "('fleet.segment_blocked','fleet.segment_pr_open','fleet.segment_merged')").fetchall()
    except sqlite3.Error:
        return seg, merged
    for r in rows:
        ts = iso(r["ts"])
        if ts is None:
            continue
        k = (r["epic"], (r["object_ref"] or "").split("|", 1)[-1], r["kind"])
        seg[k] = max(seg.get(k, 0), ts)
        if r["kind"] == "fleet.segment_merged":
            merged[r["epic"]] = max(merged.get(r["epic"], 0), ts)
    r = db.execute("SELECT MIN(ts) FROM events WHERE kind LIKE 'fleet.segment_%'").fetchone()
    merged[None] = iso(r[0]) if r and r[0] else None
    return seg, merged


SEED_KIND = {"blocked": "fleet.segment_blocked", "pr-open": "fleet.segment_pr_open", "merged": "fleet.segment_merged"}


def snapshot(db, epics, seg_hist, t, persist, asks=None):
    """{(epic, seg): since}; db is writable or None. Unseen segments are seeded from events history, else graph mtime."""
    stored = {}
    if db is not None:
        try:
            stored = {(r[0], r[1]): (r[2], r[3]) for r in db.execute("SELECT epic, seg, status, since FROM stall_snapshots")}
        except sqlite3.Error:
            pass
    since, writes = {}, []
    for e in epics:
        for s in e["segments"]:
            key, status = (e["epic"], s["id"]), s.get("status")
            old = stored.get(key)
            if old and old[0] == status:
                since[key] = old[1]
                continue
            seeded = seg_hist.get((e["epic"], s["id"], SEED_KIND.get(status))) or answered_at(s, asks or {}, status)
            since[key] = (seeded or e["mtime"] or t) if old is None else t
            writes.append((e["epic"], s["id"], status, since[key]))
    if persist and writes:
        db.executemany("INSERT OR REPLACE INTO stall_snapshots VALUES (?,?,?,?)", writes)
    return since


def answered_at(seg, asks, status):
    """Idle segments with no history: the latest answer to an ask the note names (graph mtime moves on every merge, so it is no bound)."""
    if status not in IDLE_STATUSES + ("blocked",):
        return None
    ats = [((asks.get(i) or {}).get("answer") or {}).get("at") for i in named_asks(seg)]
    return max(ats) if ats and all(ats) else None


def deps_ok(seg, by):
    for d in seg.get("deps") or []:
        s = by.get(d)
        if not s or s.get("status") not in TERMINAL:
            return False
    return True


def unmet(seg, by):
    return [d for d in seg.get("deps") or [] if (by.get(d) or {}).get("status") not in TERMINAL]


def crewed(crew, seg):
    wt = str(seg.get("worktree") or "").split("/")[-1]
    return any(a.get("cwd") and wt and a["cwd"].endswith(wt) for a in crew)


def ready_to_work(seg, by, asks, crew):
    """True when seg is neither held nor on hold, has its deps met, no PR, every named ask answered or withdrawn, and no crew on it."""
    return (seg.get("status") not in HELD and not on_hold(seg) and deps_ok(seg, by) and not seg.get("pr")
            and all(ask_resolved(asks.get(i)) for i in named_asks(seg)) and not crewed(crew, seg))


def on_hold(seg):
    """True when the newest ' | '-separated note entry says held, parked or deferred; older entries carry stale text."""
    return bool(HOLD_WORDS.search(latest_note(seg)))


def latest_note(seg):
    return (seg.get("note") or "").split(" | ")[-1]


def fingerprint(segs):
    rows = sorted(f"{s['id']}\t{s.get('status')}\t{latest_note(s)}" for s in segs)
    return hashlib.sha1("\n".join(rows).encode()).hexdigest()[:10]


def named_asks(seg):
    ptr = seg.get("ask")
    ptr = [ptr] if isinstance(ptr, str) else ptr if isinstance(ptr, list) else []
    found = [i for x in ptr if isinstance(x, str) for i in ASK_ID.findall(x)]
    return list(dict.fromkeys(found + ASK_ID.findall(seg.get("note") or "")))


def lead_target(lead, probe):
    """(pane or None, note); None means mate only and the note says why."""
    pane = lead.get("pane")
    if not pane:
        return None, " (no lead pane — mate only)"
    if probe is not None and not probe(pane, lead.get("session")):
        return None, f" (lead pane {pane} unreachable — mate only)"
    return pane, ""


class Out:
    def __init__(self):
        self.items = {}
        self.leads = {}
        self.fps = {}
        self.answered = {}

    def add(self, rule, epic, seg, text, lead=None, rows=None):
        key = ":".join(["stall", rule, epic, *([seg] if seg else [])])
        self.items[key] = text
        self.leads[key] = lead
        if rows is None:
            rows = [s for s in lead["segments"] if s.get("status") not in TERMINAL] if lead else []
        self.fps[key] = fingerprint(rows)

    def finish(self, probe):
        for k, lead in self.leads.items():
            if lead is not None and probe is not None:
                self.items[k] += lead_target(lead["lead"], probe)[1]
        return self


def listing(parts, n=6):
    return "; ".join(parts[:n]) + (f"; +{len(parts) - n} more" if len(parts) > n else "")


def evaluate(root, board, t, cfg, asks, epics, seg_hist, merged, since, probe):
    out = Out()
    agents = (board or {}).get("agents") or []
    for e in epics:
        by = {s["id"]: s for s in e["segments"]}
        ep = e["epic"]
        crew = [a for a in agents if a.get("epic") == ep and a.get("role") == "crew" and a.get("status") == "working"]
        flagged, ready = set(), []
        for s in e["segments"]:
            status, sid = s.get("status"), s["id"]
            if status not in IDLE_STATUSES or not ready_to_work(s, by, asks, crew):
                continue
            named = named_asks(s)
            states = {i: ask_state(asks.get(i)) for i in named}
            ready_at = max([since.get((ep, sid), 0)]
                           + [(by[d].get("status") in DONE and seg_hist.get((ep, d, "fleet.segment_merged"), 0)) or 0
                              for d in s.get("deps") or []]
                           + [((asks.get(i) or {}).get("answer") or {}).get("at") or 0 for i in named])
            if t - ready_at <= cfg["ready_idle"]:
                continue
            flagged.add(sid)
            why = f"{status}, asks {', '.join(f'{i} {v}' for i, v in states.items())}" if states else status
            ready.append((ready_at, f"{sid} ({why}) ready {span(t - ready_at)}", s))
        mp = e["max_parallel"]
        running = max(sum(1 for s in e["segments"] if s.get("status") in IN_FLIGHT), len(crew))
        if ready:
            ready.sort(key=lambda x: x[:2])
            cap = mp and running >= mp
            out.add("ready-idle", ep, None, f"stall ready-idle {ep}: {len(ready)} segments ready with no conductor, PR or worktree "
                    f"active ({running}/{mp or '?'} running{', at cap: lead.sh ready lends spare host slots' if cap else ': dispatch them'}): "
                    f"{listing([x[1] for x in ready])}", e)
        frozen, unblocked = [], []
        covered = set(flagged) | {s["id"] for s in e["segments"] if s.get("status") in HELD or on_hold(s)}
        for s in e["segments"]:
            status, sid = s.get("status"), s["id"]
            if status in HELD or status not in LIVE or sid in flagged or on_hold(s):
                continue
            if status == "pending" and not deps_ok(s, by):
                continue
            age_s = t - since.get((ep, sid), t)
            if age_s <= cfg["frozen"]:
                continue
            extra = ""
            if status == "blocked":
                named, um = named_asks(s), unmet(s, by)
                bits = [f"{i} {ask_state(asks.get(i))}" for i in named] + [f"dep {d} {(by.get(d) or {}).get('status', 'missing')}" for d in um]
                if s.get("pr"):
                    bits.append(f"PR #{s['pr']}")
                resolved = bool(bits) and not um and all(ask_resolved(asks.get(i)) for i in named) and not s.get("pr")
                if resolved:
                    unblocked.append((f"{sid} (blocked, blocker resolved)", s))
                extra = f"; blocker resolved: {'yes' if resolved else 'no'} ({', '.join(bits) or 'none named in note'})"
            covered.add(sid)
            frozen.append((-age_s, f"{sid} {status} {span(age_s)}{extra}", s))
        if frozen:
            frozen.sort(key=lambda x: x[:2])
            out.add("frozen", ep, None, f"stall frozen {ep}: {len(frozen)} segments unchanged over {span(cfg['frozen'])}: "
                    f"{listing([x[1] for x in frozen])}", e)
        work = [(x[1].split(" ")[0] + " (ready)", x[2]) for x in ready] + unblocked
        if work and mp and running < mp:
            out.add("under-parallel", ep, None, f"stall under-parallel {ep}: {running}/{mp} conductors running while "
                    f"{len(work)} segments could start: {listing([w[0] for w in work])}", e)
        rule2(out, root, cfg, t, asks, e, by, since, covered, flagged, crew)
        rule5(out, cfg, t, e, by, merged)
    rule6(out, root, cfg, t, epics, agents, asks)
    return out.finish(probe)


def rule2(out, root, cfg, t, asks, e, by, since, covered, flagged, crew=()):
    ep = e["epic"]
    try:
        logged = (e["dir"] / "decisions.md").read_text()
    except OSError:
        logged = ""
    named_by = {}
    for s in e["segments"]:
        if s.get("status") in LIVE or s.get("status") in HELD:
            for i in named_asks(s):
                named_by.setdefault(i, []).append(s)
    found = []
    for i, a in asks.items():
        at = (a.get("answer") or {}).get("at")
        if a.get("epic") != ep or a.get("status") != "answered" or not at or t - at <= cfg["answered"]:
            continue
        users = named_by.get(i, [])
        if users and all(unmet(s, by) for s in users):
            continue
        idle = [s for s in users if since.get((ep, s["id"]), 0) <= at]
        if idle:
            live = [s for s in idle if ready_to_work(s, by, asks, crew)]
            if live:
                out.answered.setdefault(ep, []).append({"ask": i, "at": at, "segs": [s["id"] for s in live], "record": a})
        unchanged = [s["id"] for s in idle if s["id"] not in covered]
        stale_log = i not in logged and t - at <= cfg["answered_window"] and not any(s["id"] in flagged for s in users)
        stale_log = stale_log and not (users and all(s.get("status") in HELD or on_hold(s) for s in users))
        if not unchanged and not stale_log:
            continue
        bits = (["not in decisions.md"] if i not in logged else []) + ([f"segment {', '.join(unchanged)} unchanged since"] if unchanged else [])
        found.append((at, f"{i} answered {span(t - at)} ago ({'; '.join(bits)}) {(a.get('title') or '')[:70]}"))
    if found:
        found.sort()
        out.add("answered", ep, None, f"stall answered-not-acted {ep}: {len(found)} answers not acted on: {listing([x[1] for x in found])}", e)


def rule5(out, cfg, t, e, by, merged):
    segs = [s for s in e["segments"] if s.get("status") not in GONE]
    done = [s for s in segs if s.get("status") in DONE]
    rest = [s for s in segs if s.get("status") not in DONE]
    if not segs or not rest or len(done) / len(segs) < cfg["finish_pct"]:
        return
    last = merged.get(e["epic"]) or min(x for x in (merged.get(None), e["mtime"]) if x)
    if t - last <= cfg["finish_idle"]:
        return
    depth = {}

    def d(s, seen=()):
        sid = s["id"]
        if sid not in depth:
            depth[sid] = 0 if sid in seen else 1 + max([d(by[x], seen + (sid,)) for x in s.get("deps") or [] if x in by and by[x] in rest] or [0])
        return depth[sid]

    ordered = sorted(rest, key=lambda s: (d(s), rest.index(s)))
    path = ", ".join(f"{s['id']}({s.get('status')})" for s in ordered[:8]) + (f" +{len(ordered) - 8} more" if len(ordered) > 8 else "")
    out.add("finish", e["epic"], None, f"stall finish {e['epic']}: {len(done)}/{len(segs)} ({int(100 * len(done) / len(segs))}%) "
            f"with no merge for {span(t - last)}; close-out path: {path}", e)


def promise_deadline(m, text, until):
    if until:
        return until
    p = PROMISE.search(text or "")
    if not p:
        return None
    base = datetime.fromtimestamp(m, timezone.utc)
    when = base.replace(hour=int(p.group(1)) % 24, minute=int(p.group(2)), second=0, microsecond=0).timestamp()
    return when if when >= m else when + 86400


def rule6(out, root, cfg, t, epics, agents, asks):
    by_session = {e["lead"].get("session"): e for e in epics if e["lead"].get("session")}
    live = {a.get("session") for a in agents if a.get("session")}
    for p in sorted((root / "status").glob("*.log")):
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4096))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        last = next((m for m in (LINE.match(x.strip()) for x in reversed(lines)) if m and m.group(2) in ("waiting", "progress", "done", "failed", "ended")), None)
        if not last or last.group(2) in ("done", "failed", "ended") or p.stem in live:
            continue
        at = iso(last.group(1))
        if at is None:
            continue
        deadline = promise_deadline(at, last.group(3), iso(last.group(4)) if last.group(4) else None)
        if deadline is None or t <= deadline + cfg["promise_grace"] or t - deadline > cfg["promise_max_age"]:
            continue
        if "[refreshable]" in last.group(3) or any(ask_state(asks.get(i)) == "open" for i in ASK_ID.findall(last.group(3))):
            continue
        e = by_session.get(p.stem)
        who = f"{e['epic']} lead" if e else f"session {p.stem[:8]}"
        out.add("promise", e["epic"] if e else "-", p.stem[:8],
                f"stall promise {who}: said \"{last.group(3)[:100]}\" and has not reported for {span(t - deadline)} past its deadline", e,
                [{"id": p.stem, "status": last.group(0), "note": ""}])


def memo(f):
    seen = {}
    return lambda *a: seen[a] if a in seen else seen.setdefault(a, f(*a))


def evaluate_all(root=None, board=None, t=None, write=False, probe=None, snooze=True):
    root = Path(root or ROOT)
    t = t or time.time()
    cfg = config(root)
    if probe is not None:
        probe = memo(probe)
    epics = load_epics(root)
    asks = load_asks(root)
    if write:
        db = sqlite3.connect(root / "events.db", timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        ro = db
    else:
        ro = events_ro(root)
    try:
        seg_hist, merged = history(ro)
        since = snapshot(ro, epics, seg_hist, t, write, asks)
        out = evaluate(root, board, t, cfg, asks, epics, seg_hist, merged, since, probe)
        if snooze:
            for k, v in (cfg["snooze"] if isinstance(cfg["snooze"], dict) else {}).items():
                until, fp = (v, None) if isinstance(v, str) else ((v or {}).get("until"), (v or {}).get("fp"))
                if (iso(until) or 0) > t and (fp is None or fp == out.fps.get(k)):
                    out.items.pop(k, None)
        return out
    finally:
        if ro is not None:
            ro.close()


def items(board=None, t=None, root=None, write=False, probe=None):
    """Returns {key: text}; keys are stall:<rule>:<epic>[:<seg|ask>]. write=True persists snapshots."""
    return evaluate_all(root, board, t, write, probe).items


def annotate(texts, keys, root, probe):
    """Appends the unreachable-lead note to the given keys; returns (texts, {key: lead pane or None}). Probes only these keys' leads."""
    root = Path(root or ROOT)
    probe = memo(probe) if probe is not None else None
    seen, targets, out = {}, {}, dict(texts)
    for k in keys:
        epic = k.split(":", 3)[2]
        if epic not in seen:
            lead = read_json(root / "epics" / epic / "lead.json")
            seen[epic] = lead_target(lead if isinstance(lead, dict) else {}, probe)
        targets[k] = seen[epic][0]
        out[k] = texts[k] + seen[epic][1]
    return out, targets


def deliver(keys, texts, mate_ok, send, root=None, t=None, targets=None):
    """Prompts each epic lead once per batch; logs a fire per target right after it lands (mate row first)."""
    root = Path(root or ROOT)
    t = t or time.time()
    refire_s = config(root)["refire"]
    db = sqlite3.connect(root / "events.db", timeout=30, isolation_level=None)

    def log(k, target):
        rule, epic, seg = (k.split(":", 3) + [None])[1:4]
        db.execute("INSERT INTO stall_fires VALUES (?,?,?,?,?,?,?)", (k, rule, epic, seg, t, texts[k], target))

    try:
        db.executescript(SCHEMA)
        if mate_ok:
            for k in keys:
                log(k, "mate")
        by_pane = {}
        for k in keys:
            pane = targets[k] if targets is not None else (read_json(root / "epics" / k.split(":", 3)[2] / "lead.json") or {}).get("pane")
            if pane:
                by_pane.setdefault(pane, []).append(k)
        for pane, ks in by_pane.items():
            fresh = [k for k in ks if not db.execute("SELECT 1 FROM stall_fires WHERE key=? AND ts>? AND delivered_to=?",
                                                      (k, t - refire_s, pane)).fetchone()]
            if fresh and send(pane, "\n".join([f"[watchdog] {len(fresh)} stall items for your epic:", *(texts[k] for k in fresh)])):
                for k in fresh:
                    log(k, pane)
    finally:
        db.close()


def answer_line(a):
    ans = a.get("answer") or {}
    label = next((o.get("label") for o in a.get("options") or [] if o.get("key") == ans.get("choice")), None)
    line = f"({ans.get('choice')}) {label}" if label else str(ans.get("choice"))
    return line + (f". Note: {ans['note']}" if ans.get("note") else "")


def redispatch_text(c, ep, t):
    segs = ", ".join(c["segs"])
    return (f"[redispatch] {c['ask']} was answered {span(t - c['at'])} ago and segment {segs} of {ep} has not moved since. "
            f"Answer: {answer_line(c['record'])}. {(c['record'].get('title') or '')[:100]}. "
            f"Act now: apply it to {segs} (dispatch or resume its conductor, or update graph.json status and note, and clear its ask pointer). "
            f"If it is really waiting on something else, set its status (blocked or held) with the real blocker in the note so this stops. "
            f"Answer record: adw-ask show {c['ask']}")


def retarget_aw(ep, pane):
    try:
        cmd = os.environ.get("AW_LEAD_ENROLL")
        if not cmd:
            own = Path(__file__).resolve().parent / "fleet/scripts/aw-lead-enroll"
            cmd = str(own) if own.exists() else (shutil.which("aw-lead-enroll") or "aw-lead-enroll")
        subprocess.run([cmd, "--retarget-only", ep, pane], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       timeout=20, check=False)
    except Exception:
        pass


def repoint_lead(root, ep, pane, session):
    p = root / "epics" / ep / "lead.json"
    lead = read_json(p)
    if not isinstance(lead, dict) or lead.get("pane") == pane or lead.get("session") != session:
        return False
    lead["pane"] = pane
    tmp = p.with_name(f".lead.json.{os.getpid()}")
    tmp.write_text(json.dumps(lead, indent=2))
    tmp.replace(p)
    retarget_aw(ep, pane)
    return True


def redispatch(root, t, send, resolve, notify, board=None):
    """Re-sends answers whose segment never moved: to the lead's current pane, journaled in redispatch_log; escalates to notify after redispatch_max tries.

    resolve(session, pane) -> (current pane, status) or None when the lead is gone; send(pane, text) and notify(text) return True when delivered, False on failure; send returns None when it refused to type, which records nothing.
    """
    root = Path(root)
    cfg = config(root)
    if not cfg["redispatch"]:
        return []
    got = evaluate_all(root, board, t)
    sn = cfg["snooze"] if isinstance(cfg["snooze"], dict) else {}

    def snoozed(ep):
        v = sn.get(f"stall:answered:{ep}")
        return (iso((v if isinstance(v, str) else (v or {}).get("until"))) or 0) > t

    todo = {ep: cs for ep, cs in got.answered.items() if not snoozed(ep)}
    if not todo or not (root / "events.db").exists():
        return []
    db = sqlite3.connect(root / "events.db", timeout=30, isolation_level=None)
    done, sent, notified = [], 0, 0
    try:
        db.executescript(SCHEMA)

        def log(c, ep, target, outcome, text=""):
            db.execute("INSERT INTO redispatch_log VALUES (?,?,?,?,?,?,?)", (c["ask"], ep, ",".join(c["segs"]), t, target, outcome, text))
            done.append((c["ask"], outcome))

        for ep, cands in todo.items():
            lead = read_json(root / "epics" / ep / "lead.json") or {}
            where = []
            for c in sorted(cands, key=lambda c: c["at"]):
                if t - c["at"] < cfg["redispatch_after"]:
                    continue
                rows = db.execute("SELECT ts, outcome FROM redispatch_log WHERE ask=? AND ts>? ORDER BY ts", (c["ask"], c["at"])).fetchall()
                if any(r[1] == "escalated" for r in rows) or (rows and t - rows[-1][0] < cfg["redispatch_gap"]):
                    continue
                tries = sum(1 for r in rows if r[1] in ("sent", "send-failed"))
                text = redispatch_text(c, ep, t)
                if tries >= cfg["redispatch_max"]:
                    if notified >= cfg["redispatch_notify_per_run"]:
                        continue
                    notified += 1
                    if notify(f"[redispatch] escalation: {text} It was re-sent {tries} times and the segment still has not moved."):
                        log(c, ep, "mate", "escalated", text)
                    continue
                if sent >= cfg["redispatch_per_run"]:
                    continue
                if lead and not where:
                    where.append(resolve(lead.get("session"), lead.get("pane")))
                now = where[0] if where else None
                if not now:
                    log(c, ep, None, "leaderless")
                    continue
                pane, status = now
                if status not in ("idle", "done"):
                    continue
                if pane != lead.get("pane") and repoint_lead(root, ep, pane, lead.get("session")):
                    log(c, ep, pane, "lead-repointed")
                ok = send(pane, text)
                if ok is None:
                    continue
                sent += 1
                log(c, ep, pane, "sent" if ok else "send-failed", text)
    finally:
        db.close()
    return done


def snooze_keys(root, keys, days, t=None):
    """Writes until+fingerprint for each currently firing key into root/stall.json, keeping everything else."""
    root = Path(root or ROOT)
    t = t or time.time()
    got = evaluate_all(root, t=t, snooze=False)
    missing = [k for k in keys if k not in got.items]
    if missing:
        raise SystemExit(f"not firing: {', '.join(missing)}")
    cfg = read_json(root / "stall.json")
    cfg = cfg if isinstance(cfg, dict) else {}
    sn = cfg["snooze"] if isinstance(cfg.get("snooze"), dict) else {}
    until = datetime.fromtimestamp(t + days * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for k in keys:
        sn[k] = {"until": until, "fp": got.fps[k]}
    cfg["snooze"] = sn
    tmp = root / f".stall.json.{os.getpid()}"
    tmp.write_text(json.dumps(cfg, indent=2))
    tmp.replace(root / "stall.json")
    return until


def main():
    ap = argparse.ArgumentParser(prog="stall")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--now", action="store_true", help="dry run: print the items that would fire")
    c.add_argument("--root", default=None)
    z = sub.add_parser("snooze", help="hide firing keys until their segments change or the days pass")
    z.add_argument("keys", nargs="+")
    z.add_argument("--days", type=float, required=True)
    z.add_argument("--root", default=None)
    args = ap.parse_args()
    if args.cmd == "snooze":
        until = snooze_keys(args.root, args.keys, args.days)
        print(f"snoozed {len(args.keys)} keys until {until}")
        return
    if not args.now:
        c.error("check needs --now (the live path runs from board.py watchdog)")
    got = evaluate_all(args.root)
    for k, text in got.items.items():
        print(f"{k}\n    {text}")
    print(f"stall: {len(got.items)} items would fire", file=sys.stderr)


if __name__ == "__main__":
    main()
