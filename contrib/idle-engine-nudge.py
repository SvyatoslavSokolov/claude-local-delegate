#!/usr/bin/env python3
"""PostToolUse hook: while a main session is busy working, tell it when the local
engine sits idle and hand it one concrete thing to offload in parallel.

The UserPromptSubmit pool hook only speaks when the user types, so a session
that runs for an hour on its own never hears that the engine went idle. This
hook speaks mid-run, rarely and only when it is useful:

  - never inside delegates (CLAUDE_LOCAL_DELEGATE_WORKER=1);
  - a call to any claude-local-delegate tool marks the session as "using it"
    and silences the nudge for QUIET_AFTER_DELEGATE seconds;
  - the session must be actively working (MIN_CALLS tool calls since the last
    nudge) and at least PER_SESSION_GAP seconds must have passed;
  - across all sessions at most one nudge per GLOBAL_GAP seconds, so several
    sessions do not all pile onto the single decode stream at once;
  - the engine itself must report state=idle, queue 0, model loaded.

The suggestion is concrete: files edited since the last delegation -> review_diff
on them; otherwise deep_dive / context_pack on the current open question. It is
always "launch in background, keep working, collect with wait_seconds=0".
Never fails the tool call: any error -> silent no-op.
"""
import json
import os
import sys
import time

REPO = os.path.expanduser("~/.claude/claude-local-delegate")
SETTINGS = os.path.expanduser("~/.claude/vllm.delegate.settings.json")
STATE_DIR = os.path.expanduser("~/.claude-local-delegate/nudge")
DELEGATE_PREFIX = "mcp__claude-local-delegate__"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit",
              "mcp__serena__replace_symbol_body", "mcp__serena__insert_after_symbol",
              "mcp__serena__insert_before_symbol", "mcp__serena__replace_content"}

PER_SESSION_GAP = 600       # s between nudges to one session
QUIET_AFTER_DELEGATE = 600  # s of silence after the session itself delegated
GLOBAL_GAP = 120            # s between nudges across all sessions
MIN_CALLS = 6               # tool calls since last nudge = "actively working"


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def edited_path(tool, inp):
    if tool not in EDIT_TOOLS or not isinstance(inp, dict):
        return None
    return inp.get("file_path") or inp.get("notebook_path") or inp.get("relative_path")


def engine_idle():
    try:
        sys.path.insert(0, REPO)
        import engine_status
        snap = engine_status.snapshot(SETTINGS, timeout=1.5)
    except Exception:
        return False
    return (snap.get("known") and snap.get("loaded")
            and snap.get("state") == "idle" and not snap.get("queued"))


def suggestion(files, cwd):
    if files:
        shown = ", ".join(os.path.relpath(p, cwd) if cwd and p.startswith(cwd) else p
                          for p in files[-5:])
        more = f" (+{len(files) - 5} more)" if len(files) > 5 else ""
        return (f"You changed {shown}{more} since you last used it: launch review_diff on "
                "that change now, in the background, and keep working.")
    return ("Pick the open question or the next step you are about to investigate yourself "
            "and launch deep_dive (a question) or context_pack (a task you are about to "
            "start) on it now, in the background, and keep working.")


def main():
    if os.environ.get("CLAUDE_LOCAL_DELEGATE_WORKER") == "1":
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    sid = str(payload.get("session_id") or "")
    tool = str(payload.get("tool_name") or "")
    if not sid:
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    spath = os.path.join(STATE_DIR, f"{sid}.json")
    now = time.time()
    st = load(spath, {})
    st.setdefault("start", now)

    if tool.startswith(DELEGATE_PREFIX):
        st.update(last_delegate=now, files=[], calls=0)
        save(spath, st)
        return

    st["calls"] = st.get("calls", 0) + 1
    path = edited_path(tool, payload.get("tool_input"))
    if path:
        files = [f for f in st.get("files", []) if f != path] + [path]
        st["files"] = files[-30:]

    quiet_since = max(st.get("last_nudge", 0), st.get("last_delegate", 0), st["start"])
    due = (st["calls"] >= MIN_CALLS
           and now - st.get("last_nudge", 0) >= PER_SESSION_GAP
           and now - st.get("last_delegate", 0) >= QUIET_AFTER_DELEGATE
           and now - quiet_since >= 120)
    gpath = os.path.join(STATE_DIR, "_global.json")
    if due and now - load(gpath, {}).get("last_nudge", 0) < GLOBAL_GAP:
        due = False
    if not due or not engine_idle():
        save(spath, st)
        return

    st.update(last_nudge=now, calls=0)
    save(spath, st)
    save(gpath, {"last_nudge": now, "session": sid})
    idle_for = int((now - max(st.get("last_delegate", 0), st["start"])) // 60)
    msg = ("[local-engine idle] The free local model (claude-local-delegate MCP) is idle right "
           f"now and this session has not used it for {idle_for}+ min. "
           + suggestion(st.get("files", []), payload.get("cwd") or "")
           + " Rules: it runs in parallel to you, never on your critical path -- do not wait "
           "on it, collect with get_delegate_result(wait_seconds=0) when you need it, review "
           "its output before it counts. At most 1-2 live deep runs (one decode stream shared "
           "by all sessions). If nothing in your current task fits, ignore this.")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse", "additionalContext": msg}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
