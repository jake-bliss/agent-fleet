"""Terminal map kept from `hc events`: one stream covers this machine and every connected one (`machine/t7`)."""
import importlib.util
import json
import os
import subprocess
import threading
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location("adw_mux", Path(__file__).resolve().parent / "mux.py")
mux = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mux)

STALE_S = 120
WRITE_EVERY_S = 5
RETRY_S = 2


def state_dir():
    return Path(os.environ.get("ADW_STATE_DIR") or mux.ADW_HOME / "state")


def path():
    return state_dir() / "terminals.json"


def entry(rec, session=None, changed_at=None):
    mach, local = mux.split(rec["address"])
    status = rec.get("status") if isinstance(rec.get("status"), dict) else {}
    state = "finished" if rec.get("state") == "finished" else mux.norm_status(status) if status else "unknown"
    return {"machine": mach or "local", "terminal": local, "address": rec["address"], "name": rec.get("name"),
            "cwd": rec.get("cwd") or "", "labels": rec.get("labels") or {}, "session": session,
            "agent": mux.HC_AGENTS.get(status.get("app")), "title": rec.get("title") or "", "state": state,
            "kind": status.get("kind"), "msg": status.get("msg") or "", "turn": rec.get("turn") or 0,
            "pid": (rec.get("foreground") or {}).get("pid"),
            "changed_at": changed_at or (rec.get("status_ms") or 0) / 1000 or time.time()}


class TerminalMap:
    def __init__(self, out=None):
        self.out = out
        self.lock = threading.Lock()
        self.terms = {}
        self.machines = {}
        self.last_write = 0

    def touch(self, mach, now, connected=True):
        m = self.machines.setdefault(mach, {"connected": False, "last_seen_at": 0})
        m["connected"] = connected
        if connected:
            m["last_seen_at"] = now

    def snapshot(self, data, now, connected=None):
        connected = connected or {}
        with self.lock:
            fresh = {}
            for rec in data.get("terminals") or []:
                e = entry(rec)
                e["session"] = mux.session_of(rec["address"], rec) if e["agent"] else None
                fresh[rec["address"]] = e
            seen = {e["machine"] for e in fresh.values()}
            for a, e in self.terms.items():
                if e["machine"] not in seen and connected.get(e["machine"]) is False:
                    fresh[a] = e
            self.terms = fresh
            for mach in seen:
                self.touch(mach, now)
            for mach in set(self.machines) - seen:
                self.touch(mach, now, False)
        self.persist(now, True)

    def apply(self, ev, now):
        """Returns False when the stream asks for a fresh snapshot (`missed`)."""
        method = ev.get("method")
        if method == "missed":
            self.drop(now)
            return False
        if method != "event":
            return True
        p = ev.get("params") or {}
        addr, kind, data = p.get("terminal"), p.get("type"), p.get("data") or {}
        changed = False
        again = False
        with self.lock:
            e = self.terms.get(addr)
            if e:
                self.touch(e["machine"], now)
            if e and kind == "status":
                rec = data.get("status") if isinstance(data.get("status"), dict) else data
                n = entry({"address": addr, "status": rec, "state": "live", "turn": e["turn"], "labels": e["labels"],
                           "name": e["name"], "cwd": e["cwd"], "title": e["title"]}, e["session"], now)
                n["pid"], n["agent"] = e["pid"], n["agent"] or e["agent"]
                changed = (n["state"], n["kind"], n["msg"]) != (e["state"], e["kind"], e["msg"])
                again = bool(n["agent"]) and (not e["session"] or n["state"] == "working" != e["state"])
                self.terms[addr] = {**e, **n}
            elif e and kind == "turn":
                e["turn"] = data.get("turn", e["turn"]) if isinstance(data, dict) else data
                again = bool(e["agent"]) and not e["session"]
            elif e and kind == "labels":
                e["labels"] = data.get("labels", e["labels"])
            elif e and kind in ("finished", "lost", "removed"):
                e["state"], e["changed_at"], changed = "finished", now, True
            elif kind in ("created", "restarted"):
                changed = None
        if again and changed is not None:
            rec, _ = mux.hc_json("get", addr)
            session = mux.session_of(addr, rec) if rec else None
            with self.lock:
                if session and addr in self.terms and self.terms[addr]["session"] != session:
                    self.terms[addr]["session"], changed = session, True
        if changed is None:
            rec, _ = mux.hc_json("get", addr)
            if rec:
                with self.lock:
                    self.terms[addr] = entry(rec, mux.session_of(addr, rec) if rec.get("status") else None, now)
                    self.touch(self.terms[addr]["machine"], now)
                changed = True
        self.persist(now, bool(changed))
        return True

    def heartbeat(self, now, connected):
        with self.lock:
            for mach, up in {**connected, "local": True}.items():
                self.touch(mach, now, up)
        self.persist(now, False)

    def drop(self, now):
        with self.lock:
            for mach in self.machines:
                self.machines[mach]["connected"] = False
        self.persist(now, True)

    def view(self):
        with self.lock:
            return {"written_at": time.time(), "machines": json.loads(json.dumps(self.machines)),
                    "terminals": json.loads(json.dumps(list(self.terms.values())))}

    def persist(self, now, force):
        if not self.out or not force and now - self.last_write < WRITE_EVERY_S:
            return
        self.last_write = now
        write(self.out, self.view())


def write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(data))
    tmp.replace(p)


def load(p=None):
    try:
        data = json.loads((p or path()).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("terminals"), list) else None


def stale_machines(view, now):
    return {m for m, s in (view.get("machines") or {}).items()
            if not s.get("connected") or now - (s.get("last_seen_at") or 0) > STALE_S}


def machine_states():
    data, _ = mux.hc_json("machine", "ls")
    out = {m["name"]: m.get("state") == "connected" for m in (data or {}).get("machines") or []}
    return out


def stream(mp, lines, clock=time.time, machines=machine_states):
    """Feeds one `hc events` stream into the map; returns when it ends or asks for a fresh snapshot."""
    first = True
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        now = clock()
        if first:
            if "terminals" not in ev:
                return
            first = False
            states = machines()
            mp.snapshot(ev, now, states)
            mp.heartbeat(now, states)
        elif ev.get("method") == "heartbeat":
            mp.heartbeat(now, machines())
        elif not mp.apply(ev, now):
            return


def run(mp, sleep=time.sleep):
    while True:
        try:
            proc = subprocess.Popen([mux.bin_path(), "events", "--json"], stdout=subprocess.PIPE, text=True)
            try:
                stream(mp, proc.stdout)
            finally:
                proc.kill()
                proc.wait()
        except Exception:
            pass
        mp.drop(time.time())
        sleep(RETRY_S)


def start(out=None):
    mp = TerminalMap(out or path())
    threading.Thread(target=run, args=(mp,), daemon=True).start()
    return mp


def agent_row(e, status=None):
    return {"pane_id": e["address"], "agent": e["agent"], "agent_status": status or e["state"], "cwd": e["cwd"],
            "terminal_title_stripped": e["title"], "name": e["name"], "machine": e["machine"] if e["machine"] != "local" else None,
            "status_msg": e["msg"], "tab_id": None, "backend": "hc", "agent_session": {"value": e["session"]},
            "changed_at": e["changed_at"]}


def agents(live=None, now=None, seen_path=None):
    """list_agents-shaped rows from the live map or the persisted file, or None when neither exists. Terminals of a
    stale machine are 'unknown' for the live board; the file path (readers outside the board process) asks hc ls for them once instead."""
    now = now or time.time()
    view = live.view() if live else load()
    if view is None or (not live and (not view["terminals"] or now - (view.get("written_at") or 0) > STALE_S)):
        return None
    stale = stale_machines(view, now)
    live_terms = [e for e in view["terminals"] if e["agent"] and e["state"] != "finished"]
    if live:
        return [agent_row(e, "unknown" if e["machine"] in stale else None) for e in live_terms]
    rows = [agent_row(e) for e in live_terms if e["machine"] not in stale]
    data, _ = mux.hc_json("ls") if stale else (None, None)
    if not stale or data is None:
        return rows + [agent_row(e, "unknown") for e in live_terms if e["machine"] in stale]
    seen_p = seen_path or state_dir() / "watchdog-seen.json"
    seen = load_seen(seen_p)
    old = {e["address"]: e for e in view["terminals"]}
    fresh = {}
    for t in data.get("terminals") or []:
        addr, kind = t["address"], mux.HC_AGENTS.get(t.get("agent"))
        mach = mux.split(addr)[0] or "local"
        if mach not in stale or not kind:
            continue
        state = mux.norm_status(t.get("status"))
        prior = seen.get(addr) or {}
        fresh[addr] = prior if prior.get("state") == state else {"state": state, "since": now}
        e = old.get(addr) or {}
        rows.append(agent_row({"address": addr, "agent": kind, "state": state, "cwd": t.get("cwd") or "",
                               "title": e.get("title", ""), "name": t.get("name"), "machine": mach, "msg": "",
                               "session": e.get("session"), "changed_at": fresh[addr]["since"]}))
    write(seen_p, {**{k: v for k, v in seen.items() if (mux.split(k)[0] or "local") not in stale}, **fresh})
    return rows


def load_seen(p):
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
