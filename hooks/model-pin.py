#!/usr/bin/env python3
import json, sys

try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
inp = data.get("tool_input") if isinstance(data, dict) else None
if not isinstance(inp, dict):
    sys.exit(0)
kind = inp.get("subagent_type") or "general-purpose"
model = (inp.get("model") or "").lower()
if "# model-ok" in (inp.get("prompt") or ""):
    sys.exit(0)

why = None
if kind in ("adw-conductor", "worker") and model and "sonnet" not in model:
    why = (f"{kind} is pinned to Sonnet in its agent definition; drop `model: \"{model}\"`. "
           "Opus conductors/workers cost ~2x for the same /adw flow.")
elif kind == "Explore" and model != "haiku":
    why = "Explore always runs with `model: \"haiku\"` (delegation rule); otherwise it inherits the orchestrator's model."
if why:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": why + " If the override is truly needed, add `# model-ok <reason>` to the prompt."}}))
