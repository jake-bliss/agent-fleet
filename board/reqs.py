#!/usr/bin/env python3
"""Request queue: ad-hoc asks from coworkers and clients, tracked until the asker is told.
Usage: req add "<what>" --who <person> [--client C] [-p P0-P3] [--due YYYY-MM-DD] [--link URL] [--owner t7|epic]
       req ls [--all] [--json]       open requests, priority then due then age
       req show <id>
       req set <id> [--title|--who|--client|-p|--due|--link|--owner ...]   ("" or --due none clears)
       req note <id> "<text>"
       req doing|wait|done|told|drop <id> ["<note>"]
A request stays open until `told` (the asker heard back) or `drop`; `done` means the work is done
and you or a tab still owe the asker a reply."""
import argparse
import fcntl
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

STATUSES = ("new", "doing", "waiting", "done", "told", "dropped")
CLOSED = ("told", "dropped")
PRIORITIES = ("P0", "P1", "P2", "P3")
STALE_HOURS = {"P0": 4, "P1": 24, "P2": 72, "P3": None}
ID = re.compile(r"r-\d+")


def state_dir():
    return Path(os.environ.get("ADW_STATE_DIR") or Path(os.environ.get("ADW_HOME") or Path.home() / ".claude/adw") / "state")


def path():
    return state_dir() / "requests.json"


def read():
    try:
        return json.loads(path().read_text())
    except FileNotFoundError:
        return {"next": 1, "items": []}


@contextmanager
def locked():
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_suffix(".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        d = read()
        yield d
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1) + "\n")
        os.replace(tmp, p)


def actor():
    return os.environ.get("ADW_REQ_BY") or os.environ.get("HC_TERMINAL_ID") or "cli"


def is_open(r):
    return r["status"] not in CLOSED


def stale(r, now=None):
    hours = STALE_HOURS.get(r["priority"])
    if not hours or not is_open(r) or r["status"] == "waiting":
        return False
    return (now or time.time()) - r["updated"] >= hours * 3600


def overdue(r, today=None):
    return bool(r.get("due")) and is_open(r) and r["status"] != "done" and r["due"] < (today or date.today().isoformat())


def ordered(items):
    return sorted(items, key=lambda r: (PRIORITIES.index(r["priority"]), r.get("due") or "9999", r["created"]))


def open_items():
    return ordered([r for r in read()["items"] if is_open(r)])


def find(d, rid):
    r = next((r for r in d["items"] if r["id"] == rid), None)
    if not r:
        raise SystemExit(f"req: no request {rid}")
    return r


def touch(r, note=None):
    r["updated"] = time.time()
    if note:
        r["notes"].append({"at": r["updated"], "by": actor(), "text": note})


def due_arg(s):
    if s in ("", "none"):
        return ""
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("due must be YYYY-MM-DD")


def link_arg(s):
    if s and not s.startswith(("https://", "http://")):
        raise argparse.ArgumentTypeError("link must be a URL")
    return s


def age(sec):
    h = sec / 3600
    return f"{int(sec // 60)}m" if h < 1 else f"{int(h)}h" if h < 48 else f"{int(h // 24)}d"


def line(r, now=None):
    now = now or time.time()
    flags = [f for f, on in (("STALE", stale(r, now)), ("OVERDUE", overdue(r))) if on]
    who = r["who"] + (f" ({r['client']})" if r.get("client") else "")
    bits = [r["id"], r["priority"], r["status"], who, r["title"]]
    tail = [f"due {r['due']}" if r.get("due") else "", f"owner {r['owner']}" if r.get("owner") else "",
            f"{age(now - r['created'])} old", " ".join(flags)]
    return "  ".join(bits) + "  ·  " + " · ".join(t for t in tail if t)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(prog="req", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    fields = argparse.ArgumentParser(add_help=False)
    fields.add_argument("--who")
    fields.add_argument("--client")
    fields.add_argument("-p", "--priority", type=str.upper, choices=PRIORITIES)
    fields.add_argument("--due", type=due_arg)
    fields.add_argument("--link", type=link_arg)
    fields.add_argument("--owner")
    a_add = sub.add_parser("add", parents=[fields])
    a_add.add_argument("title")
    a_ls = sub.add_parser("ls")
    a_ls.add_argument("--all", action="store_true")
    a_ls.add_argument("--json", action="store_true")
    sub.add_parser("show").add_argument("id")
    a_set = sub.add_parser("set", parents=[fields])
    a_set.add_argument("id")
    a_set.add_argument("--title")
    a_set.add_argument("--status", choices=STATUSES)
    a_note = sub.add_parser("note")
    a_note.add_argument("id")
    a_note.add_argument("text")
    for s in ("doing", "wait", "done", "told", "drop"):
        p = sub.add_parser(s)
        p.add_argument("id")
        p.add_argument("text", nargs="?")
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0

    if a.cmd == "add":
        if not a.who:
            print("req: --who is required (the person who asked)", file=sys.stderr)
            return 2
        with locked() as d:
            now = time.time()
            r = {"id": f"r-{d['next']}", "title": a.title, "who": a.who, "client": a.client,
                 "priority": a.priority or "P2", "due": a.due or None, "status": "new", "owner": a.owner,
                 "link": a.link, "created": now, "updated": now, "by": actor(), "notes": []}
            d["next"] += 1
            d["items"].append(r)
        print(line(r))
        return 0
    if a.cmd == "ls":
        items = ordered(read()["items"]) if a.all else open_items()
        if a.json:
            now = time.time()
            print(json.dumps([{**r, "stale": stale(r, now), "overdue": overdue(r)} for r in items]))
        else:
            print("\n".join(line(r) for r in items) if items else "no open requests")
        return 0
    if not ID.fullmatch(a.id):
        print(f"req: bad id {a.id!r} (want r-<n>)", file=sys.stderr)
        return 2
    if a.cmd == "show":
        r = find(read(), a.id)
        print(line(r))
        if r.get("link"):
            print(f"link: {r['link']}")
        for n in r["notes"]:
            print(f"- {time.strftime('%m-%d %H:%M', time.localtime(n['at']))} {n['by']}: {n['text']}")
        return 0
    with locked() as d:
        r = find(d, a.id)
        if a.cmd == "note":
            touch(r, a.text)
        elif a.cmd == "set":
            given = {k: getattr(a, k) for k in ("title", "who", "client", "priority", "due", "link", "owner", "status")
                     if getattr(a, k) is not None}
            given.update({k: None for k in ("client", "due", "link", "owner") if given.get(k) == ""})
            if not all(given.get(k, "x") for k in ("title", "who")):
                print("req: --title and --who cannot be empty", file=sys.stderr)
                return 2
            changed = [k for k, v in given.items() if v != r.get(k)]
            for k in changed:
                r[k] = given[k]
            touch(r, "set " + ", ".join(f"{k}={r[k]}" for k in changed) if changed else None)
        else:
            r["status"] = {"wait": "waiting", "drop": "dropped"}.get(a.cmd, a.cmd)
            touch(r, a.text or a.cmd)
    print(line(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
