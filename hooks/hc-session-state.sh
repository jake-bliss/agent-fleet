#!/bin/sh
# SessionStart/SessionEnd hook: records the Claude session id of an hc terminal under
# $ADW_HOME/hc-sessions/<ADW_HC_MACHINE, else mux.machine(), else local>/<HC_TERMINAL_ID>. Usage: hc-session-state.sh start|end
set -eu

action="${1:-}"
case "$action" in start|end) ;; *) exit 0 ;; esac
[ -n "${HC_TERMINAL_ID:-}" ] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

HC_ACTION="$action" python3 -c '
import json
import os
import re
import sys

terminal = os.environ["HC_TERMINAL_ID"]
adw_home = os.path.expanduser(os.environ.get("ADW_HOME") or "~/.claude/adw")
sys.path.insert(0, os.path.join(adw_home, "board"))
machine = os.environ.get("ADW_HC_MACHINE")
if not machine:
    try:
        import mux
        machine = mux.machine() or "local"
    except Exception:
        machine = "local"
if not re.fullmatch(r"t\w+", terminal) or not re.fullmatch(r"[\w.-]+", machine):
    raise SystemExit(0)
path = os.path.join(os.environ.get("ADW_SESSIONS_DIR") or os.path.join(adw_home, "hc-sessions"), machine, terminal)
if os.environ["HC_ACTION"] == "end":
    try:
        os.remove(path)
    except OSError:
        pass
    raise SystemExit(0)
try:
    data = json.load(sys.stdin)
    session = data.get("session_id")
    if not data.get("agent_id") and isinstance(session, str) and re.fullmatch(r"[0-9a-f-]{8,64}", session):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as out:
            out.write(session + "\n")
except Exception:
    pass
' || true
