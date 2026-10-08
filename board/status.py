#!/usr/bin/env python3
"""adw-status: declare what this agent tab is doing, so the ADW Board can tell waiting from stalled.

Usage:
  adw-status waiting "<what>" [--until 2026-01-01T22:00Z | --for 30m] [--refreshable]
  adw-status progress "<text>"
  adw-status done "<text>"
  adw-status failed "<text>"
  adw-status show [--pane PANE]
  adw-status lead <epic>            # register this tab as the epic's lead (writes epics/<epic>/lead.json)

Lines use the github.com/wolzey/chartroom event format: `<ISO-8601 UTC> <kind>: <text>[ until <ISO>]`.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ADW_HOME = Path(os.environ.get("ADW_HOME") or Path.home() / ".claude/adw")
STATUS = ADW_HOME / "status"
EPICS = ADW_HOME / "epics"
KINDS = ("waiting", "progress", "done", "failed")
LINE = re.compile(r"^(\S+) (\w+): (.*?)(?: until (\S+))?$")


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def session_for(pane):
    r = subprocess.run(["herdr", "agent", "get", pane], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return None
    a = json.loads(r.stdout)["result"]
    a = a.get("agent", a)
    return (a.get("agent_session") or {}).get("value")


def session_live(session):
    r = subprocess.run(["herdr", "agent", "list"], capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        return False
    return any((x.get("agent_session") or {}).get("value") == session for x in json.loads(r.stdout)["result"]["agents"])


def rebind_asks(epic, prev, session, pane):
    import importlib.util
    spec = importlib.util.spec_from_file_location("asks", Path(__file__).resolve().parent / "asks.py")
    asks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(asks)
    try:
        logged = (EPICS / epic / "decisions.md").read_text()
    except OSError:
        logged = ""
    if not prev:
        return 0
    moved = 0
    with asks.locked():
        for d in asks.all_asks():
            if d.get("epic") != epic or d.get("session") != prev:
                continue
            unacked = d["status"] == "answered" and d["id"] not in logged
            if d["status"] != "open" and not unacked:
                continue
            d.update(session=session, pane=pane)
            if unacked:
                for k in ("delivered", "undeliverable", "delivery_error", "attempts", "last_attempt"):
                    d.pop(k, None)
            asks.save(d)
            moved += 1
    asks.retry_deliveries()
    return moved


def last_event(session):
    p = STATUS / f"{session}.log"
    if not session or not p.exists():
        return None
    with open(p, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 8192))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        m = LINE.match(line.strip())
        if m and m.group(2) in KINDS:
            return {"at": parse_iso(m.group(1)), "kind": m.group(2), "text": m.group(3),
                    "until": parse_iso(m.group(4)) if m.group(4) else None}
    return None


def duration(s):
    m = re.fullmatch(r"(\d+)([mhd])", s)
    if not m:
        raise SystemExit(f"--for wants e.g. 30m, 2h, 1d; got {s!r}")
    unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
    return timedelta(**{unit: int(m.group(1))})


def main():
    ap = argparse.ArgumentParser(prog="adw-status")
    ap.add_argument("kind", choices=KINDS + ("show", "lead"))
    ap.add_argument("text", nargs="?", default="")
    ap.add_argument("--until")
    ap.add_argument("--for", dest="for_")
    ap.add_argument("--pane")
    ap.add_argument("--refreshable", action="store_true")
    ap.add_argument("--takeover", action="store_true")
    a = ap.parse_args()
    pane = a.pane or os.environ.get("HERDR_PANE_ID")
    if not pane:
        raise SystemExit("adw-status: not in a herdr pane (HERDR_PANE_ID unset); nothing recorded")
    session = session_for(pane)
    if not session:
        raise SystemExit(f"adw-status: no agent session found in pane {pane}")
    if a.kind == "show":
        print(json.dumps(last_event(session), indent=2))
        return
    if a.kind == "lead":
        epic = a.text.strip()
        if not epic or not (EPICS / epic / "graph.json").exists():
            raise SystemExit(f"adw-status lead: no epic {epic!r} under {EPICS}")
        try:
            prev = json.loads((EPICS / epic / "lead.json").read_text())
        except (OSError, ValueError):
            prev = {}
        if prev.get("session") and prev["session"] != session and not a.takeover and session_live(prev["session"]):
            raise SystemExit(f"adw-status lead: {prev.get('pane')} is still the live lead of {epic}; "
                             "pass --takeover only if the human asked for a new lead")
        rec = {"pane": pane, "session": session, "at": iso(datetime.now(timezone.utc))}
        tmp = EPICS / epic / f".lead.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(rec, indent=2))
        tmp.replace(EPICS / epic / "lead.json")
        moved = rebind_asks(epic, prev.get("session"), session, pane) if prev.get("session") != session else 0
        print(f"{pane} is now the lead of {epic}" + (f"; {moved} decision(s) re-routed here" if moved else ""))
        return
    text = " ".join(a.text.split())
    if not text:
        raise SystemExit("adw-status: say what, e.g. adw-status waiting \"CI on the acme-api PR\" --for 30m")
    if a.refreshable:
        text += " [refreshable]"
    line = f"{iso(datetime.now(timezone.utc))} {a.kind}: {text}"
    if a.kind == "waiting":
        if a.until:
            until = datetime.fromisoformat(a.until.replace("Z", "+00:00"))
            if until.tzinfo is None:
                until = until.astimezone()
            line += f" until {iso(until)}"
        elif a.for_:
            line += f" until {iso(datetime.now(timezone.utc) + duration(a.for_))}"
    STATUS.mkdir(parents=True, exist_ok=True)
    with open(STATUS / f"{session}.log", "a") as f:
        f.write(line + "\n")
    print(line)


if __name__ == "__main__":
    main()
