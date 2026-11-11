#!/usr/bin/env python3
"""adw-ask: file a decision for the human on the ADW Board and get the answer back in this pane.

Usage:
  adw-ask new --title T [--epic E] [--body-file F | --body TEXT | --body -]
              [--option KEY=LABEL]... [--recommend KEY]        prints the decision id
              --class merge|prod-write|money|auth|customer-data|scope|irreversible|reversible|design|split|redesign|fix-streak|size|sequencing
              [--default-after 4h]   non-gated decisions with no gated wording: apply --recommend if unanswered by then
              A non-gated ask with --recommend and no gated wording is answered at once (by "auto").
  adw-ask list [--all] [--json]
  adw-ask show <id>
  adw-ask wait <id> [--timeout SECONDS]                       blocks until answered; prints the answer JSON
  adw-ask answer <id> <choice> [--note TEXT] [--relay "<the human's words>" | --by mate]
  adw-ask withdraw <id>
"""
import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ADW_HOME = Path(os.environ.get("ADW_HOME") or Path.home() / ".claude/adw")
ASKS = ADW_HOME / "asks"
_spec = importlib.util.spec_from_file_location("adw_mux", Path(__file__).resolve().parent / "mux.py")
mux = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mux)


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


def configured_machines():
    raw = os.environ.get("ADW_MACHINES")
    if raw is None:
        try:
            raw = ",".join(json.loads((ADW_HOME / "machines.json").read_text()).get("machines", []))
        except (OSError, ValueError, AttributeError, TypeError):
            raw = ""
    return [m.strip() for m in raw.split(",") if re.fullmatch(r"\w+", m.strip())]


REMOTE_DIR = Path(os.environ.get("ADW_HERDR_REMOTE_DIR") or Path.home() / ".config/herdr/remote")
REMOTE_HERDR = {m: REMOTE_DIR / f"{m}.sock" for m in configured_machines()}
MACHINE = os.environ.get("ADW_MACHINE")
REMOTE_PANE = re.compile(r"(\w+)/(w\w+:p\w+)")


def live_remotes():
    return [m for m, s in REMOTE_HERDR.items() if m != MACHINE and s.is_socket()]


def pane_ref(pane):
    m = mux.machine() if mux.is_hc(pane) else MACHINE
    return f"{m}/{pane}" if m and pane and "/" not in pane else pane


def herdr_cmd(args, machine=None):
    args = list(args)
    m = REMOTE_PANE.fullmatch(args[2]) if len(args) > 2 and args[0] in ("agent", "pane") else None
    if m and m.group(1) == MACHINE:
        args[2] = m.group(2)
    elif m and m.group(1) in REMOTE_HERDR:
        machine, args[2] = m.group(1), m.group(2)
    env = {**os.environ, "HERDR_SOCKET_PATH": str(REMOTE_HERDR[machine])} if machine else None
    return ["herdr", *args], env


def herdr(*args, machine=None):
    cmd, env = herdr_cmd(args, machine)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()[:300]
    return r.stdout, None


def agent_info(pane):
    if mux.is_hc(pane):
        return mux.get(pane)
    out, _ = herdr("agent", "get", pane)
    if not out:
        return None
    a = json.loads(out)["result"]
    a = a.get("agent", a)
    return {"status": a.get("agent_status"),
            "session": (a.get("agent_session") or {}).get("value")}


def prompt(pane, text, guard=None):
    if mux.is_hc(pane):
        return mux.prompt(pane, text, guard=guard)
    refused = guard() if guard else None
    return (None, refused) if refused else herdr("agent", "prompt", pane, text)


def read_pane(pane, lines=200):
    if mux.is_hc(pane):
        return mux.read(pane, lines)
    return herdr("agent", "read", pane, "--source", "recent-unwrapped", "--lines", str(lines))


def send_keys(pane, keys):
    return mux.keys(pane, keys) if mux.is_hc(pane) else herdr("pane", "send-keys", pane, *keys)


def focus(pane):
    return mux.focus(pane) if mux.is_hc(pane) else herdr("agent", "focus", pane)


def own_pane():
    return mux.own_pane() or os.environ.get("HERDR_PANE_ID")


def list_agents():
    """Agents of every available backend in the herdr `agent list` shape, or None when neither backend answers."""
    agents, ok = [], False
    if shutil.which("herdr"):
        for machine in [None, *live_remotes()]:
            out, _ = herdr("agent", "list", machine=machine)
            if out is None:
                continue
            ok = True
            for a in json.loads(out)["result"]["agents"]:
                agents.append({**a, "pane_id": f"{machine}/{a['pane_id']}", "machine": machine} if machine else a)
    if mux.available():
        hc_agents = mux.list_agents()
        if hc_agents is not None:
            ok = True
            agents += hc_agents
    return agents if ok else None


GATED = ("merge", "prod-write", "money", "auth", "customer-data", "scope", "irreversible")
NON_GATED = ("reversible", "design", "split", "redesign", "fix-streak", "size", "sequencing")
PR_SHAPE_CLASSES = ("size", "split")
_DANGER = (r"publish|release|\bdrop\b|prod|deploy|delete|drop |refund|price|pricing|charge|credential|secret|token|migrat|rollback"
           r"|payout|api[- ]?key|password|purge|wipe|truncat|tear[- ]?down|void(?:ing)? invoice|issu(?:e|ing) credit"
           r"|admin access|disabl(?:e|ing) mfa")
GATE_SCAN = re.compile(r"ship \S+#\d+|\bship\b|pr-ship|\bland\b|squash|merge|\bmerg|" + _DANGER, re.I)
PR_MERGE_TOKEN = re.compile(r"\bship\s+\S*#\d+|pr-ship", re.I)
GATE_SCAN_NO_PR_SHAPE = re.compile(_DANGER, re.I)
SHIP_TOKEN = re.compile(r"ship \S+#\d+@[0-9a-f]+", re.I)


def gate_hit(d):
    opts = [t for o in d.get("options") or [] for t in (o.get("key"), o.get("label"))]
    if d.get("class") in NON_GATED:
        texts = [d.get("title"), *opts]
    else:
        texts = [d.get("title"), d.get("body"), d.get("relay"), (d.get("answer") or {}).get("relay"), *opts]
    relaxed = d.get("class") in PR_SHAPE_CLASSES
    for t in texts:
        m = (SHIP_TOKEN.search(t or "") or GATE_SCAN_NO_PR_SHAPE.search(t or "")) if relaxed else GATE_SCAN.search(t or "")
        if m:
            return m.group(0)
    return None


def gate_reason(d):
    """Why this ask may not be answered without the human, or None. Re-run at use time for every auto/default answer."""
    if d.get("class") in GATED:
        return f"class {d['class']} is gated"
    hit = gate_hit(d)
    return f"text mentions {hit.strip()!r}" if hit else None


def answer_who(a):
    by = a.get("by") or "human"
    if by == "default":
        return "No answer in time; the recommended default was applied", "Note"
    if by == "auto":
        return "Auto-applied the recommended option (reversible, not gated)", "Note"
    if by == "human":
        return "the human chose", "Note from the human"
    if by == "relay:human":
        return "the human chose (relayed by the first mate)", "Note from the human"
    return f"{by} chose", "Note"


def answer_message(d):
    a = d["answer"]
    label = next((o["label"] for o in d["options"] if o["key"] == a["choice"]), None)
    choice = f"({a['choice']}) {label}" if label else a["choice"]
    who, note_label = answer_who(a)
    msg = f"[adw-ask {d['id']} answered] {d['title']} -> {who}: {choice}"
    if a.get("note"):
        msg += f". {note_label}: {a['note']}"
    return msg + f". Full record: adw-ask show {d['id']}"


MAX_ATTEMPTS = 3
RESEND_AFTER = 600


def marker(d):
    return f"[adw-ask {d['id']} answered]"


def seen_by_agent(d):
    out, _ = read_pane(d["pane"])
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
    if d["answer"].get("by") == f"agent:{d['pane']}":
        d["delivered"] = now()
        d.pop("delivery_error", None)
        return merge_delivery(d)
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
    if d.get("session") and (info is None or info["session"] != d["session"]):
        moved = relocate(d)
        if moved:
            d, info = moved, agent_info(moved["pane"])
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
        _, err = prompt(d["pane"], answer_message(d))
        if err:
            d["delivery_error"] = f"prompt failed: {err}; will retry"
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


def relocate(d):
    """Follows the asking session to the pane it occupies now; returns the moved record or None."""
    new = mux.terminal_for_session(d.get("session")) if d.get("session") else None
    new = pane_ref(new) if new else None
    if not new or new == d["pane"] or not migrate_pane(d["pane"], new, d["session"]):
        return None
    return load(d["id"])


def migrate_pane(old, new, session):
    """Moves open decisions and answered ones never delivered from pane old to new, for this session only."""
    moved = 0
    with locked():
        for d in all_asks():
            if d.get("pane") != old or d.get("session") != session:
                continue
            undelivered = d["status"] == "answered" and not d.get("delivered")
            if d["status"] != "open" and not undelivered:
                continue
            d["pane"] = new
            if undelivered:
                for k in DELIVERY_FIELDS:
                    d.pop(k, None)
            save(d)
            moved += 1
    return moved


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


def record_answer(i, choice, note="", by="human", relay=None):
    choice = (choice or "").strip()
    if not choice and not note.strip():
        raise ValueError("pick an option or write a note")
    with locked():
        d = load(i)
        if d["status"] != "open":
            raise ValueError(f"{i} is {d['status']}")
        if by == "default" and not (d.get("default_at") and d["default_at"] <= now()):
            raise ValueError(f"{i} default not due")
        if by in ("auto", "default") and (why := gate_reason(d)):
            if by == "default" and d.get("default_blocked") != why:
                d["default_blocked"] = why
                save(d)
            raise ValueError(f"{i} {by} answer blocked: {why}")
        d["status"] = "answered"
        d["answer"] = {"choice": choice or "note", "note": note.strip(), "at": now(), "by": by}
        if relay:
            d["answer"]["relay"] = relay.strip()
        save(d)
    return deliver(d)


SHIP_LABEL = re.compile(r"^ship\s+([A-Za-z0-9._-]+)#([1-9]\d*)@[0-9a-f]{7,40}", re.I)
def ship_repos():
    """{short repo name: owner/repo} for the repos in pr-tools.json, the names a `ship <repo>#<n>@<sha>` label uses."""
    path = os.environ.get("PR_TOOLS_CONFIG") or str(ADW_HOME / "pr-tools.json")
    try:
        repos = (json.loads(Path(path).read_text()).get("repos") or {}).keys()
    except (OSError, ValueError, AttributeError):
        return {}
    return {r.split("/", 1)[1].lower(): r for r in repos if "/" in r}


def ships_without_ask(options):
    """Returns the pr-ship dry-run line when plain pr-ship would act on the PR in a ship option, else None. Any error fails open."""
    for o in options:
        m = SHIP_LABEL.match((o.get("label") or "").strip())
        repo = m and ship_repos().get(m.group(1).lower())
        exe = shutil.which("pr-ship")
        if not repo or not exe:
            continue
        try:
            r = subprocess.run([exe, m.group(2), "-R", repo, "--dry-run"], capture_output=True, text=True, timeout=90)
        except (OSError, subprocess.SubprocessError):
            continue
        line = (r.stdout.strip().splitlines() or [""])[-1]
        if r.returncode == 0 and " dry-run: would " in line:
            return line
    return None


def body_hash(body):
    return hashlib.sha1((body or "").encode()).hexdigest()


def default_by(pane):
    return "agent:" + (pane_ref(pane) if pane else "cli")


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
    cls = a.cls
    if not cls:
        raise SystemExit(f"--class is required; gated (the human decides): {', '.join(GATED)}; non-gated (auto-applied with --recommend): {', '.join(NON_GATED)}")
    if cls not in GATED + NON_GATED:
        raise SystemExit(f"unknown --class {a.cls!r}; one of: {', '.join(GATED + NON_GATED)}")
    if cls in NON_GATED and cls not in PR_SHAPE_CLASSES:
        hit = next((m.group(0) for t in [a.title, *(o["label"] for o in options)] if (m := PR_MERGE_TOKEN.search(t or ""))), None)
        if hit:
            raise SystemExit(f"a {cls} ask can't carry a PR merge ({hit!r}): it would wait for the human instead of applying. "
                             "Ask only the fix/design choice here; the merge goes through `pr-ship` (or a separate --class merge ask if it refuses).")
    if cls == "merge" and (line := ships_without_ask(options)):
        raise SystemExit(f"no ask needed: plain pr-ship acts on this PR ({line}). Run `pr-ship <pr>`; file an ask only when it refuses or waits for the human.")
    pane = own_pane()
    d = {"id": "d-" + secrets.token_hex(3), "created": now(), "status": "open",
         "title": a.title, "epic": a.epic, "body": body, "options": options, "class": cls,
         "recommend": a.recommend, "pane": pane_ref(pane), "session": None,
         "cwd": os.getcwd(), "default_at": None}
    why = gate_reason(d)
    if why:
        d["gated_reason"] = why
    if a.default_after:
        m = re.fullmatch(r"(\d+)([mh])", a.default_after)
        if not m or not a.recommend:
            raise SystemExit("--default-after wants e.g. 90m or 4h, and needs --recommend")
        if why:
            raise SystemExit(f"--default-after refused: {why}; the human must decide this one")
        secs = int(m.group(1)) * (60 if m.group(2) == "m" else 3600)
        if secs < 1800:
            raise SystemExit("--default-after must give the human at least 30m")
        d["default_at"] = now() + secs
    info = agent_info(pane) if pane else None
    session = info and info["session"]

    def sig(o, session):
        return (o.get("epic"), o.get("pane"), session, o.get("class"), o.get("recommend"), o["title"],
                [(x["key"], x["label"]) for x in o["options"]], body_hash(o["body"]))

    with locked():
        dup = next((o for o in all_asks() if o["status"] == "open" and sig(o, o.get("session")) == sig(d, session)), None)
        if dup:
            print(dup["id"])
            return
        d["session"] = session
        save(d)
    print(d["id"])
    if not pane:
        print("warning: not in a terminal pane; the answer can only be read with `adw-ask wait`", file=sys.stderr)
    if cls in NON_GATED and a.recommend and why:
        print(f"note: stays open for the human because the title or an option mentions {why.split(chr(39))[1]!r}; "
              f"reword it to be auto-applied (default-after cannot override this scan)", file=sys.stderr)
    if cls in NON_GATED and a.recommend and not why:
        try:
            record_answer(d["id"], a.recommend, "", by="auto")
        except ValueError as e:
            print("already answered" if "is answered" in str(e) else str(e), file=sys.stderr)
            return
        print(f"auto-applied {a.recommend}", file=sys.stderr)


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
    if a.relay and a.by:
        raise SystemExit("--relay and --by are exclusive")
    by = "relay:human" if a.relay else a.by or default_by(own_pane())
    try:
        d = record_answer(a.id, a.choice, a.note or "", by=by, relay=a.relay)
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
    n.add_argument("--class", dest="cls")
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
    an.add_argument("--relay")
    an.add_argument("--by", choices=("mate",))
    a = ap.parse_args()
    {"new": cmd_new, "list": cmd_list, "show": cmd_show, "wait": cmd_wait,
     "answer": cmd_answer, "withdraw": cmd_withdraw}[a.cmd](a)


if __name__ == "__main__":
    main()
