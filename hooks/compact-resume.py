#!/usr/bin/env python3
"""SessionStart(compact|resume) hook: re-inject the tab's in-flight state from disk.

Reads only files ADW already keeps (status/<session>.log, asks/*.json, epics/*/lead.json +
graph.json) plus the session transcript for the user's last messages verbatim, and prints a short
resume card as additionalContext. Never fails the session: any error prints nothing.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ADW = Path(os.environ.get("ADW_HOME") or Path.home() / ".claude/adw")
MAX_CARD = 3500
MAX_MSG = 400
TERMINAL = ("merged", "deployed", "done", "dropped", "cancel", "closed", "superseded", "folded",
            "invalid", "retired", "split", "reclassified", "executed", "decomposed", "resolved",
            "decided")


def last_status(session):
    f = ADW / "status" / f"{session}.log"
    if not f.is_file():
        return None
    lines = f.read_text(errors="ignore").strip().splitlines()
    if not lines:
        return None
    when = when_of(lines[-1].split(" ", 1)[0])
    age = f" ({(datetime.now(timezone.utc) - when).total_seconds() / 3600:.1f}h ago)" if when else ""
    return lines[-1] + age


def when_of(v):
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, timezone.utc)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None


def asks(session):
    open_, recent = [], []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=3)
    found = {}
    for f in (ADW / "asks").glob("d-*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if d.get("session") == session:
            found[d.get("id")] = d
    for d in found.values():
        if d.get("status") == "open":
            open_.append(f"{d['id']} {d.get('title', '')[:120]}")
        elif d.get("status") == "answered":
            ans = d.get("answer") or {}
            at = ans.get("at") if isinstance(ans, dict) else None
            when = when_of(at)
            if when is None:
                continue
            if when >= cutoff:
                choice = ans.get("choice", "")
                note = (ans.get("note") or "")[:100]
                recent.append(f"{d['id']} -> {choice}{': ' + note if note else ''}")
    return open_, recent


def led_epic(session):
    for f in (ADW / "epics").glob("*/lead.json"):
        try:
            if json.loads(f.read_text()).get("session") == session:
                return f.parent
        except (OSError, ValueError):
            continue
    return None


def open_segments(epic_dir):
    try:
        segs = json.loads((epic_dir / "graph.json").read_text()).get("segments", [])
    except (OSError, ValueError):
        return []
    out = []
    for s in segs:
        st = str(s.get("status", ""))
        if st.startswith(TERMINAL):
            continue
        pr = f" #{s['pr']}" if s.get("pr") else ""
        note = f" ({str(s['note'])[:80]})" if s.get("note") else ""
        out.append(f"{s.get('id')} {st}{pr}{note}")
    return out


def git_line(cwd):
    def git(*a):
        return subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, timeout=3).stdout.strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if not branch:
        return None
    dirty = len([l for l in git("status", "--porcelain").splitlines() if l.strip()])
    return f"{cwd}: branch {branch}, {dirty} uncommitted file(s)"


def user_text(d):
    if d.get("type") != "user" or d.get("isMeta") or d.get("isCompactSummary"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
        if len(parts) != len(c):
            return None
        c = "\n".join(parts)
    if not isinstance(c, str):
        return None
    c = c.strip()
    if not c or c.startswith("<") or c.startswith("This session is being continued"):
        return None
    return c


def last_user_messages(transcript, n=3):
    if not transcript or not os.path.isfile(transcript):
        return []
    msgs = []
    with open(transcript, errors="ignore") as fh:
        for line in fh:
            if '"type":"user"' not in line:
                continue
            try:
                t = user_text(json.loads(line))
            except ValueError:
                continue
            if t:
                msgs.append(t)
    out = []
    for m in msgs[-n:]:
        out.append(m if len(m) <= MAX_MSG else m[:MAX_MSG] + " [...]")
    return out


def card(inp):
    session = inp.get("session_id") or ""
    rows = []
    st = last_status(session)
    if st:
        rows.append(f"- Your last adw-status: {st}")
    epic = led_epic(session)
    if epic:
        segs = open_segments(epic)
        rows.append(f"- You lead epic {epic.name}: state in {epic}/graph.json and decisions.md; reconcile from them.")
        if segs:
            rows.append("- Non-terminal segments: " + "; ".join(segs[:15]) + (f"; +{len(segs) - 15} more" if len(segs) > 15 else ""))
    open_, recent = asks(session)
    if open_:
        rows.append("- Your open adw-asks (wait for the answer, do not re-ask): " + "; ".join(open_[:8]))
    if recent:
        rows.append("- Your asks answered in the last 3h (reference only; graph.json and the summary say what is already done): " + "; ".join(recent[:5]))
    g = git_line(inp.get("cwd") or os.getcwd())
    if g:
        rows.append(f"- {g}")
    msgs = last_user_messages(inp.get("transcript_path"))
    if msgs:
        rows.append("- Last user messages, verbatim (oldest first):")
        rows += [f"  > {m.replace(chr(10), ' / ')}" for m in msgs]
    if not rows:
        return None
    text = "Resume card (compact-resume hook, read from disk after compaction; the files named here are the source of truth):\n" + "\n".join(rows)
    return text[:MAX_CARD]


def main():
    try:
        inp = json.load(sys.stdin)
        if inp.get("source") not in ("compact", "resume"):
            return
        text = card(inp)
    except Exception:
        return
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))


if __name__ == "__main__":
    main()
