#!/usr/bin/env python3
"""UserPromptSubmit hook: inject a compact local-model delegate-pool status into
context on every user turn, so the main session never has to be told "check what
the agents are doing" and never forgets the local model is idle.

Reads the hook JSON on stdin (for session_id, to exclude self). Calls
`claude agents --json` (cheap, no model). Emits additionalContext:
  - count of running delegate agents + their names
  - any BLOCKED agents, by name, flagged for an answer
  - an IDLE warning when running < target (env CLAUDE_DELEGATE_TARGET, default 3)
Never fails the turn: any error -> silent no-op.
"""
import json
import os
import subprocess
import sys

TARGET = int(os.environ.get("CLAUDE_DELEGATE_TARGET", "3"))
RUNNING = {"running", "working", "busy", "in_progress"}
BLOCKED = {"blocked", "waiting", "needs_input"}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    self_sid = str(payload.get("session_id") or "")

    try:
        p = subprocess.run(["claude", "agents", "--json"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20)
        out = p.stdout.decode("utf-8", "replace")
        agents = json.loads(out[out.find("["):])
    except Exception:
        return  # supervisor down / no roster -> say nothing

    running, blocked = [], []
    for a in agents:
        sid = str(a.get("sessionId") or a.get("session_id") or "")
        if self_sid and (sid == self_sid or sid.startswith(self_sid) or self_sid.startswith(a.get("id", "\0"))):
            continue
        if a.get("kind") == "interactive":
            continue  # only background delegates
        st = (a.get("state") or a.get("status") or "").lower()
        name = a.get("name") or a.get("id") or "?"
        if st in BLOCKED:
            blocked.append(name)
        elif st in RUNNING:
            running.append(name)

    lines = [f"Local-model delegate pool: {len(running)} running"
             + (f" ({', '.join(running[:6])})" if running else "")
             + f", {len(blocked)} blocked."]
    if blocked:
        lines.append(f"BLOCKED delegates need an answer NOW: {', '.join(blocked)}. "
                     f"Use watch_delegate then SendMessage / stop_delegate.")
    if len(running) < TARGET:
        lines.append(f"Local GPU is under-loaded ({len(running)}/{TARGET}). Per standing "
                     f"instruction: split off mechanical/independent work and delegate_to_local "
                     f"(or fan_out_to_local) until at least {TARGET} delegates are running, before "
                     f"doing that work yourself.")
    else:
        lines.append(f"Pool at target ({len(running)}/{TARGET}); glance at watch_delegate for any drift.")

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "[delegate-pool] " + " ".join(lines),
    }}))


if __name__ == "__main__":
    main()
