#!/usr/bin/env python3
"""UserPromptSubmit hook: inject a compact local-model delegate-pool status into
context on every user turn, so the main session never has to be told "check what
the agents are doing" and never forgets the local model is idle.

Reads the hook JSON on stdin (for session_id, to exclude self). Calls
`claude agents --json` (cheap, no model). Emits additionalContext:
  - count of running delegate agents + their names
  - any BLOCKED agents, by name, flagged for an answer
  - the engine's real state (requests it serves at once, queue, cached
    conversations) and how many more deep delegates fit, from engine_status
  - an OFFLINE notice instead when the local backend does not answer, so the
    main session stops trying to delegate locally and routes to itself / AGY
Never fails the turn: any error -> silent no-op.
"""
import json
import os
import subprocess
import sys
import urllib.request

REPO = os.path.expanduser("~/.claude/claude-local-delegate")
RUNNING = {"running", "working", "busy", "in_progress"}
BLOCKED = {"blocked", "waiting", "needs_input"}
SETTINGS = os.path.expanduser("~/.claude/vllm.delegate.settings.json")


def local_run_ids():
    """Ids this MCP spawned on the local backend (provenance), or None if unreadable."""
    state = os.environ.get("CLAUDE_LOCAL_DELEGATE_STATE_DIR", os.path.expanduser("~/.claude-local-delegate"))
    try:
        with open(os.path.join(state, "runs.json")) as f:
            runs = json.load(f).get("runs", {})
    except Exception:
        return None
    return {rid for rid, e in runs.items() if isinstance(e, dict) and e.get("backend") == "local"}


def backend_online():
    """True/False when the gateway answers or not; None when unknown (no profile)."""
    try:
        with open(SETTINGS) as f:
            base = json.load(f)["env"]["ANTHROPIC_BASE_URL"]
    except Exception:
        return None
    try:
        urllib.request.urlopen(base, timeout=2)
    except urllib.error.HTTPError:
        return True  # it answered, just not with 200
    except Exception:
        return False
    return True


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

    local_ids = local_run_ids()
    running, blocked = [], []
    for a in agents:
        if local_ids is not None and a.get("id") not in local_ids:
            continue  # paid background sessions do not load the local engine
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
    online = backend_online()
    if online is False:
        lines.append("Local backend is OFFLINE (gateway does not answer): do not call "
                     "delegate_to_local or the deep tools now. Do the work on the main model, "
                     "or send self-contained mid-size tasks to delegate_to_agy.")
    else:
        advice = ""
        try:
            sys.path.insert(0, REPO)
            import engine_status
            advice = engine_status.advice(engine_status.snapshot(SETTINGS, timeout=2), len(running))
        except Exception:
            pass
        lines.append(advice or "Local engine state unknown.")
        if not running:
            lines.append("Deep mode: hand the local model work you would otherwise do yourself -- "
                         "context_pack before a task, deep_dive on an open question, review_diff on "
                         "every non-trivial change -- and keep working while it runs.")

    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "[delegate-pool] " + " ".join(lines),
    }}))


if __name__ == "__main__":
    main()
