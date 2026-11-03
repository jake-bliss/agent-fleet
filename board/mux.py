"""hc (herdr-core, the herdr app) backend for the terminal seam: ids look like t7 (this machine) or <machine>/t7."""
import contextlib
import fcntl
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

HC_ID = re.compile(r"(?:[\w.-]+/)?t\w+")
PANE_PATTERN = r"(?:[\w.-]+/)?t\w+"
PANE_ID = re.compile(PANE_PATTERN)
ADW_HOME = Path(os.environ.get("ADW_HOME") or Path.home() / ".claude/adw")
CONFIG = ADW_HOME / "mux.json"
HC_AGENTS = {"claude-code": "claude", "codex": "codex"}
STATUSES = {"idle": "idle", "working": "working", "done": "done"}
TARGETED = ("get", "send", "keys", "read", "interrupt", "wait", "label", "kill", "rm")
NO_FOCUS = "focus is not supported for hc terminals"


def config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {}


def is_hc(pane):
    return bool(HC_ID.fullmatch(pane or ""))


valid = is_hc


def machine():
    host = socket.gethostname().split(".")[0]
    return os.environ.get("ADW_HC_MACHINE") or (host if host in (config().get("hc_nodes") or []) else None)


def own_pane():
    """The caller's own terminal id as this box's core knows it (bare), or None. Resumed sessions carry a stale
    HERDR_PANE_ID, so it is never read."""
    if os.environ.get("HC_TERMINAL_ID"):
        return os.environ["HC_TERMINAL_ID"]
    session = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    return terminal_for_session(session) if session else None


def terminal_for_session(session):
    """The terminal whose hc-sessions record holds this Claude session id, bare for this box's own core."""
    root = sessions_dir()
    if not session or not root.is_dir():
        return None
    def mtime(rec):
        try:
            return rec.stat().st_mtime
        except OSError:
            return 0
    for rec in sorted(root.glob("*/t*"), key=mtime, reverse=True):
        try:
            if rec.read_text().strip() != session:
                continue
        except OSError:
            continue
        pane = strip_self(rec.parent.name + "/" + rec.name if rec.parent.name != "local" else rec.name)
        live, _ = hc_json("get", pane)
        if live and live.get("state") != "finished":
            return pane
    return None


def sessions_dir():
    return Path(os.environ.get("ADW_SESSIONS_DIR") or ADW_HOME / "hc-sessions")


def split(pane):
    m, _, local = (pane or "").rpartition("/")
    return m or None, local


def strip_self(pane):
    m, local = split(pane)
    return local if is_hc(pane) and m and m == machine() else pane


def bin_path():
    for c in (os.environ.get("HC_BIN"), config().get("hc_bin")):
        if c:
            return c
    found = shutil.which("herdr-core") or shutil.which("hc")
    if found:
        return found
    for c in ("~/.local/bin/herdr-core", "~/.herdr/bin/herdr-core", "~/.cargo/bin/herdr-core"):
        p = Path(c).expanduser()
        if p.exists():
            return str(p)
    return "herdr-core"


def available():
    b = bin_path()
    return bool(shutil.which(b) or Path(b).exists())


def hc(*args, timeout=15):
    if os.environ.get("ADW_MUX_TEST") and (os.environ.get("HC_TERMINAL_SOCKET") or os.environ.get("HC_SCOPE")
                                           or Path(bin_path()).name != "fake-hc"):
        raise RuntimeError("test would reach a real hc core")
    if args[:1] and args[0] in TARGETED and len(args) > 1:
        args = (args[0], strip_self(args[1]), *args[2:])
    try:
        r = subprocess.run([bin_path(), *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if r.returncode != 0:
        return None, (r.stderr or r.stdout).strip()[:300] or f"hc {args[0]} exited {r.returncode}"
    return r.stdout, None


def hc_json(*args):
    out, err = hc(*args, "--json")
    if out is None:
        return None, err
    try:
        return json.loads(out), None
    except ValueError as e:
        return None, str(e)


def norm_status(value):
    """ls gives a string, get gives {app, state}. Anything that is not idle, working or done is treated as blocked."""
    state = value.get("state") if isinstance(value, dict) else value
    return STATUSES.get(state, "blocked")


def status_of(rec):
    return norm_status(rec.get("status"))


def read_file(path, mach):
    """Reads a file in the home of the box the terminal lives on; relative paths start at that box's home."""
    if mach and mach != machine():
        out, _ = hc("file", "read", "--on", mach, path)
        return out
    try:
        return (Path.home() / path).read_text()
    except OSError:
        return None


def session_from_pid(pid, mach):
    if not pid:
        return None
    out = read_file(f".claude/sessions/{pid}.json", mach)
    try:
        return json.loads(out).get("sessionId") if out else None
    except (ValueError, AttributeError):
        return None


RECORD_DIR = ".claude/adw/hc-sessions"


def record_machine(mach):
    return mach or machine() or "local"


def ssh_target(mach):
    data, _ = hc_json("machine", "ls")
    return next((m.get("target") for m in (data or {}).get("machines") or [] if m.get("name") == mach), None)


def ssh_cat(target, path):
    if os.environ.get("ADW_MUX_TEST"):
        raise RuntimeError("test would reach ssh")
    try:
        r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target, "cat", path],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def read_session_record(terminal, mach):
    """The Claude session id recorded for a terminal by the hook on the machine that owns it, or None. This machine's
    records and those of machines listed in mux.json "shared_machines" (a shared ADW_HOME) are local files; any other
    machine is read over ssh."""
    name = record_machine(mach)
    local = sessions_dir() / name / terminal
    if not mach or mach == machine() or mach in (config().get("shared_machines") or []):
        try:
            return local.read_text().strip() or None
        except OSError:
            return None
    target = ssh_target(mach)
    out = ssh_cat(target, f"~/{RECORD_DIR}/{name}/{terminal}") if target else None
    return (out or "").strip() or None


def session_from_argv(rec):
    argv = rec.get("argv") or (rec.get("foreground") or {}).get("argv") or []
    for i, a in enumerate(argv[:-1]):
        if a == "--resume":
            return argv[i + 1]
    return None


def session_of(address, rec):
    mach, local = split(strip_self(address))
    pid = (rec.get("foreground") or {}).get("pid")
    return session_from_pid(pid, mach) or read_session_record(local, mach) or session_from_argv(rec)


def get(pane):
    """Returns {"status", "session"} for an hc terminal, or None."""
    rec, _ = hc_json("get", pane)
    if not rec or rec.get("state") == "finished":
        return None
    return {"status": status_of(rec), "session": session_of(pane, rec)}


def list_agents():
    """Returns agents as dicts (pane_id, agent, agent_status, cwd, name, machine, backend="hc", agent_session), or None when hc is unavailable."""
    data, _ = hc_json("ls")
    if data is None:
        return None
    out = []
    for t in data.get("terminals") or []:
        kind = HC_AGENTS.get(t.get("agent"))
        if not kind:
            continue
        addr = t["address"]
        rec, _ = hc_json("get", addr)
        mach, _ = split(addr)
        out.append({"pane_id": addr, "agent": kind, "agent_status": norm_status(t.get("status")),
                    "cwd": t.get("cwd") or "", "terminal_title_stripped": (rec or {}).get("title") or "",
                    "name": t.get("name"), "machine": mach, "status_msg": ((rec or {}).get("status") or {}).get("msg") or "", "tab_id": None, "backend": "hc",
                    "agent_session": {"value": session_of(addr, rec or {})}})
    return out


PLAIN_KEYS = re.compile(r"[a-z0-9/.-]")


def key_names(text):
    """Key names that type text, or None when a character has no key name (hc keys cannot type "_" or most punctuation)."""
    names = []
    for ch in text:
        if ch == " ":
            names.append("space")
        elif PLAIN_KEYS.fullmatch(ch):
            names.append(ch)
        elif re.fullmatch(r"[A-Z]", ch):
            names.append(f"shift+{ch.lower()}")
        else:
            return None
    return names


LOCK_WAIT = 30


@contextlib.contextmanager
def terminal_lock(pane, timeout=None):
    """Exclusive per-terminal lock held while typing, so concurrent senders cannot interleave keys. Raises TimeoutError."""
    root = Path(os.environ.get("ADW_LOCK_DIR") or ADW_HOME / "locks")
    root.mkdir(parents=True, exist_ok=True)
    with open(root / f"hc-{re.sub(r'[^A-Za-z0-9.-]', '_', strip_self(pane))}.lock", "w") as f:
        deadline = time.monotonic() + (LOCK_WAIT if timeout is None else timeout)
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{pane} is busy with another sender")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def prompt(pane, text, timeout=None):
    try:
        with terminal_lock(pane, timeout):
            return type_text(pane, text)
    except TimeoutError as e:
        return None, str(e)


def type_text(pane, text):
    """Sends text and presses enter. A one-line slash command is typed key by key: pasted text reaches Claude as a
    plain message, so the command would never run."""
    if not (text.startswith("/") and "\n" not in text):
        return hc("send", pane, text)
    names = key_names(text)
    if names is None:
        return None, "cannot type this slash command through hc keys"
    info = get(pane)
    if not info or info["status"] == "blocked":
        return None, "terminal is blocked or gone; not typing into it"
    for step in (("esc",), tuple(names)):
        out, err = hc("keys", pane, *step)
        if err:
            return None, err
    time.sleep(1)
    return hc("keys", pane, "enter")


def read(pane, lines):
    return hc("read", pane, "--lines", str(lines))


def keys(pane, names):
    return hc("keys", pane, *names)


def focus(pane):
    return None, NO_FOCUS


def run_address(out, on):
    try:
        d = json.loads(out)
    except ValueError:
        d = {}
    addr = d.get("address") or d.get("terminal") or (out.strip().split() or [None])[-1]
    if addr and on and "/" not in addr:
        addr = f"{on}/{addr}"
    return addr


def start(name, cwd, argv, space=None, on=None):
    """Starts a detached terminal; returns (address, None) or (None, error)."""
    cmd = ["run", "--bg", "--no-parent", "--outlive-parent", "--name", name, "--cwd", str(cwd)]
    if space or config().get("hc_space"):
        cmd += ["--label", f"herdr.space={space or config()['hc_space']}"]
    if on:
        cmd += ["--on", on]
    out, err = hc(*cmd, "--json", "--", *argv, timeout=60)
    if out is None:
        return None, err
    addr = run_address(out, on)
    return (addr, None) if addr else (None, "hc run printed no address")


def wait_idle(pane, timeout=60, sleep=time.sleep):
    """Polls until the terminal's agent reports idle; False when it never does within the timeout."""
    deadline = time.monotonic() + timeout
    while True:
        info = get(pane)
        if info and info["status"] == "idle":
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(1)
