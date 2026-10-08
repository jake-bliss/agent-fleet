#!/usr/bin/env python3
"""adw-ask: file a decision for the human on the ADW Board and get the answer back in this pane.

Usage:
  adw-ask new --title T [--epic E] [--body-file F | --body TEXT | --body -]
              [--option KEY=LABEL]... [--recommend KEY]        prints the decision id
              [--default-after 4h]   reversible decisions only: apply --recommend if unanswered by then
  adw-ask list [--all] [--json]
  adw-ask show <id>
  adw-ask wait <id> [--timeout SECONDS]                       blocks until answered; prints the answer JSON
  adw-ask answer <id> <choice> [--note TEXT]
  adw-ask withdraw <id>
"""
import argparse
import contextlib
import fcntl
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

ADW_HOME = Path(os.environ.get("ADW_HOME") or Path.home() / ".claude/adw")
ASKS = ADW_HOME / "asks"


def now():
    return int(time.time())


def path(i):
    if not re.fullmatch(r"d-[0-9a-f]{6}", i or ""):
        raise SystemExit(f"bad decision id: {i!r}")
    return ASKS / f"{i}.json"


def load(i):
    p = path(i)
    if not p.exists():
        raise SystemExit(f"no such decision: {i}")
    return json.loads(p.read_text())


@contextlib.contextmanager
def locked():
    ASKS.mkdir(parents=True, exist_ok=True)
    with open(ASKS / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def save(d):
    ASKS.mkdir(parents=True, exist_ok=True)
    p = path(d["id"])
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(p)


def all_asks():
    if not ASKS.exists():
        return []
    out = []
    for p in ASKS.glob("d-*.json"):
        try:
            out.append(json.loads(p.read_text()))
        except ValueError:
            pass
    return sorted(out, key=lambda d: d["created"])


def herdr(*args):
    try:
        r = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()[:300]
    return r.stdout, None


def agent_info(pane):
    out, _ = herdr("agent", "get", pane)
    if not out:
        return None
    a = json.loads(out)["result"]
    a = a.get("agent", a)
    return {"status": a.get("agent_status"),
            "session": (a.get("agent_session") or {}).get("value")}


def answer_message(d):
    a = d["answer"]
    label = next((o["label"] for o in d["options"] if o["key"] == a["choice"]), None)
    choice = f"({a['choice']}) {label}" if label else a["choice"]
    who = "No answer in time; the recommended default was applied" if a.get("by") == "default" else "the human chose"
    msg = f"[adw-ask {d['id']} answered] {d['title']} -> {who}: {choice}"
    if a.get("note"):
        msg += f". Note from the human: {a['note']}"
    return msg + f". Full record: adw-ask show {d['id']}"


MAX_ATTEMPTS = 3
RESEND_AFTER = 600


def marker(d):
    return f"[adw-ask {d['id']} answered]"


def seen_by_agent(d):
    out, _ = herdr("agent", "read", d["pane"], "--source", "recent-unwrapped", "--lines", "200")
    if out and marker(d) in out:
        return True
    if d.get("session"):
        for p in (Path.home() / ".claude/projects").glob(f"*/{d['session']}.jsonl"):
            with open(p, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 524288))
                if marker(d).encode() in f.read():
                    return True
    return False


DELIVERY_FIELDS = ("delivered", "delivery_error", "undeliverable", "attempts", "last_attempt")


def merge_delivery(d):
    with locked():
        cur = load(d["id"])
        if ((cur.get("session"), cur.get("pane")) != (d.get("session"), d.get("pane")) or cur.get("delivered")
                or (cur.get("last_attempt") or 0) > (d.get("last_attempt") or 0)):
            return cur
        for k in DELIVERY_FIELDS:
            if k in d:
                cur[k] = d[k]
            else:
                cur.pop(k, None)
        save(cur)
        return cur


def deliver(d):
    if d["status"] != "answered" or d.get("delivered") or d.get("undeliverable") or not d.get("pane"):
        return d
    if d.get("attempts") and seen_by_agent(d):
        d["delivered"] = now()
        d.pop("delivery_error", None)
        return merge_delivery(d)
    if d.get("attempts", 0) and now() - d.get("last_attempt", 0) < RESEND_AFTER:
        return d
    if d.get("attempts", 0) >= MAX_ATTEMPTS:
        d["undeliverable"] = True
        d["delivery_error"] = f"sent {MAX_ATTEMPTS} times but never seen in the agent's tab; answer kept in the record"
        return merge_delivery(d)
    info = agent_info(d["pane"])
    if info is None or (d.get("session") and info["session"] != d["session"]):
        d["undeliverable"] = True
        d["delivery_error"] = "the asking agent no longer occupies its pane; answer kept in the record"
    elif info["status"] == "blocked":
        # Text typed now would land in the agent's approval/question dialog.
        d["delivery_error"] = "agent is in a dialog; will retry"
    else:
        with locked():
            cur = load(d["id"])
            if (cur.get("delivered") or (cur.get("last_attempt") or 0) > (d.get("last_attempt") or 0)
                    or (cur.get("session"), cur.get("pane")) != (d.get("session"), d.get("pane"))):
                return cur
            cur["attempts"] = cur.get("attempts", 0) + 1
            cur["last_attempt"] = now()
            save(cur)
            d = cur
        _, err = herdr("agent", "prompt", d["pane"], answer_message(d))
        if err:
            d["delivery_error"] = f"herdr prompt failed: {err}; will retry"
        else:
            for _ in range(6):
                time.sleep(1)
                if seen_by_agent(d):
                    d["delivered"] = now()
                    d.pop("delivery_error", None)
                    break
            else:
                d["delivery_error"] = "sent; waiting to see it in the agent's tab"
    return merge_delivery(d)


def retry_deliveries():
    for d in all_asks():
        if d["status"] == "answered" and not d.get("delivered") and not d.get("undeliverable"):
            deliver(d)


def apply_defaults():
    for d in all_asks():
        if d["status"] == "open" and d.get("default_at") and d["default_at"] <= now() and d.get("recommend"):
            try:
                record_answer(d["id"], d["recommend"], "", by="default")
            except ValueError:
                pass


def record_answer(i, choice, note="", by="human"):
    choice = (choice or "").strip()
    if not choice and not note.strip():
        raise ValueError("pick an option or write a note")
    with locked():
        d = load(i)
        if d["status"] != "open":
            raise ValueError(f"{i} is {d['status']}")
        if by == "default" and not (d.get("default_at") and d["default_at"] <= now()):
            raise ValueError(f"{i} default not due")
        d["status"] = "answered"
        d["answer"] = {"choice": choice or "note", "note": note.strip(), "at": now(), "by": by}
        save(d)
    return deliver(d)


def cmd_new(a):
    if a.body_file:
        body = Path(a.body_file).read_text()
    elif a.body == "-":
        body = sys.stdin.read()
    else:
        body = a.body or ""
    options = []
    for o in a.option or []:
        k, _, label = o.partition("=")
        if not label:
            raise SystemExit(f"--option needs KEY=LABEL, got {o!r}")
        options.append({"key": k.strip(), "label": label.strip()})
    if a.recommend and a.recommend not in {o["key"] for o in options}:
        raise SystemExit(f"--recommend {a.recommend!r} is not an option key")
    default_at = None
    if a.default_after:
        m = re.fullmatch(r"(\d+)([mh])", a.default_after)
        if not m or not a.recommend:
            raise SystemExit("--default-after wants e.g. 90m or 4h, and needs --recommend")
        secs = int(m.group(1)) * (60 if m.group(2) == "m" else 3600)
        if secs < 1800:
            raise SystemExit("--default-after must allow at least 30m")
        default_at = now() + secs
    pane = os.environ.get("HERDR_PANE_ID")
    info = agent_info(pane) if pane else None
    d = {"id": "d-" + secrets.token_hex(3), "created": now(), "status": "open",
         "title": a.title, "epic": a.epic, "body": body, "options": options,
         "recommend": a.recommend, "pane": pane, "session": info and info["session"],
         "cwd": os.getcwd(), "default_at": default_at}
    save(d)
    print(d["id"])
    if not pane:
        print("warning: not in a herdr pane; the answer can only be read with `adw-ask wait`", file=sys.stderr)


def cmd_list(a):
    ds = [d for d in all_asks() if a.all or d["status"] == "open"]
    if a.json:
        print(json.dumps(ds, indent=2))
        return
    for d in ds:
        print(f"{d['id']}  {d['status']:<9} {d.get('epic') or '-':<28} {d['title']}")


def cmd_show(a):
    print(json.dumps(load(a.id), indent=2))


def cmd_wait(a):
    end = time.time() + a.timeout if a.timeout else None
    while True:
        d = load(a.id)
        if d["status"] != "open":
            print(json.dumps({"status": d["status"], "answer": d.get("answer")}))
            return
        if end and time.time() > end:
            raise SystemExit(f"timeout waiting for {a.id}")
        time.sleep(5)


def cmd_answer(a):
    try:
        d = record_answer(a.id, a.choice, a.note or "")
    except ValueError as e:
        raise SystemExit(str(e))
    print("delivered" if d.get("delivered") else d.get("delivery_error", "recorded"))


def cmd_withdraw(a):
    d = load(a.id)
    d["status"] = "withdrawn"
    d["withdrawn"] = now()
    save(d)
    print(f"{a.id} withdrawn")


def main():
    ap = argparse.ArgumentParser(prog="adw-ask")
    sp = ap.add_subparsers(dest="cmd", required=True)
    n = sp.add_parser("new")
    n.add_argument("--title", required=True)
    n.add_argument("--epic")
    n.add_argument("--body")
    n.add_argument("--body-file")
    n.add_argument("--option", action="append")
    n.add_argument("--recommend")
    n.add_argument("--default-after")
    ls = sp.add_parser("list")
    ls.add_argument("--all", action="store_true")
    ls.add_argument("--json", action="store_true")
    for name in ("show", "withdraw"):
        sp.add_parser(name).add_argument("id")
    w = sp.add_parser("wait")
    w.add_argument("id")
    w.add_argument("--timeout", type=float)
    an = sp.add_parser("answer")
    an.add_argument("id")
    an.add_argument("choice")
    an.add_argument("--note")
    a = ap.parse_args()
    {"new": cmd_new, "list": cmd_list, "show": cmd_show, "wait": cmd_wait,
     "answer": cmd_answer, "withdraw": cmd_withdraw}[a.cmd](a)


if __name__ == "__main__":
    main()
