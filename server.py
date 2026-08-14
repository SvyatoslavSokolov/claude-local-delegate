#!/usr/bin/env python3
"""
MCP server for delegating tasks to a local model, by spawning a real
headless `claude -p` subprocess pointed at a local backend (via --settings).
The delegated call gets the actual Claude Code agent loop (tools, context
management, CLAUDE.md) -- not a hand-rolled prompt wrapper -- just running
against a local model instead of Anthropic's.

Delegation is asynchronous: delegate_to_local spawns the subprocess in the
background and returns a run_id immediately, so the calling session isn't
blocked for the minutes a local generation can take. check_delegate_status
and get_delegate_result poll for progress/output. Because each call returns
immediately, firing off several delegate_to_local calls back-to-back runs
them genuinely in parallel -- bounded by the local server's own concurrency
limit (e.g. vLLM's --max-num-seqs), not by this tool.

Stdlib-only: no `mcp` SDK dependency, so no venv/pip install is needed.
Implements the MCP stdio transport directly (newline-delimited JSON-RPC 2.0).
"""

import json
import os
import subprocess
import sys
import time
import uuid

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "claude-local-delegate"
SERVER_VERSION = "0.2.0"

DEFAULT_SETTINGS_PATH = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_SETTINGS",
    os.path.expanduser("~/.claude/vllm.settings.json"),
)
DEFAULT_ALLOWED_TOOLS = "Read,Grep,Glob"
RUNS_DIR = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_RUNS_DIR",
    os.path.expanduser("~/.claude-local-delegate/runs"),
)
LOG_TAIL_CHARS = 4000

TOOLS = [
    {
        "name": "delegate_to_local",
        "description": (
            "Start a self-contained task on the local model (served by vLLM) "
            "by spawning a real headless `claude -p` subprocess with --settings "
            "pointed at the local backend. The local model gets the full Claude "
            "Code agent loop (its own tool calls, its own context management) "
            "for this task, not a summarized prompt. Returns immediately with a "
            "run_id -- this does NOT block waiting for the local model, so you "
            "can call this multiple times back-to-back to run several local "
            "delegations in parallel (limited by the local server's own "
            "concurrency, e.g. vLLM --max-num-seqs). Poll with "
            "check_delegate_status, then read the answer with get_delegate_result. "
            "Use for mechanical / high-volume work (drafts, boilerplate, "
            "straightforward refactors, summarization) where local-model latency "
            "is worth saving tokens on the main session. Do not use for tasks "
            "needing careful judgment, architecture decisions, or "
            "security-sensitive changes -- keep those in the main session."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task/prompt to hand to the local model. Be specific and self-contained -- the local sub-session starts with no memory of this conversation.",
                },
                "allowed_tools": {
                    "type": "string",
                    "description": (
                        "Comma-separated tools the local sub-session may use without "
                        "prompting, e.g. 'Read,Edit,Write,Bash,Grep,Glob'. Defaults to "
                        f"read-only ('{DEFAULT_ALLOWED_TOOLS}'). Widen only when the task "
                        "genuinely needs to write files or run commands -- the local "
                        "model runs unsupervised with whatever access you grant here."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory for the local sub-session. Defaults to this MCP server's own working directory.",
                },
                "resume_session_id": {
                    "type": "string",
                    "description": "Optional session_id from a prior delegate_to_local run (see get_delegate_result), to continue that local sub-session instead of starting fresh.",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "check_delegate_status",
        "description": (
            "Check whether a delegate_to_local run (by run_id) is still running, "
            "completed, or failed. Returns a tail of its log while running."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "run_id returned by delegate_to_local."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "get_delegate_result",
        "description": (
            "Fetch the final result of a completed delegate_to_local run. "
            "Returns an error if the run is still in progress -- call "
            "check_delegate_status first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "run_id returned by delegate_to_local."},
            },
            "required": ["run_id"],
        },
    },
]


def _run_dir(run_id):
    return os.path.join(RUNS_DIR, run_id)


def start_delegate(args):
    task = args.get("task")
    if not task or not isinstance(task, str):
        return _error_result("`task` is required and must be a non-empty string.")

    allowed_tools = args.get("allowed_tools") or DEFAULT_ALLOWED_TOOLS
    cwd = args.get("cwd") or os.getcwd()
    resume_id = args.get("resume_session_id")

    if not os.path.isfile(DEFAULT_SETTINGS_PATH):
        return _error_result(
            f"Local settings file not found: {DEFAULT_SETTINGS_PATH}. "
            "Set CLAUDE_LOCAL_DELEGATE_SETTINGS or create the file."
        )

    run_id = uuid.uuid4().hex[:12]
    run_dir = _run_dir(run_id)
    os.makedirs(run_dir, exist_ok=True)

    with open(os.path.join(run_dir, "prompt.md"), "w") as f:
        f.write(task)

    cmd = [
        "claude", "-p", task,
        "--settings", DEFAULT_SETTINGS_PATH,
        "--allowedTools", allowed_tools,
        "--output-format", "stream-json",
        "--verbose",
    ]
    if resume_id:
        cmd += ["--resume", resume_id]

    log_path = os.path.join(run_dir, "output.log")
    exit_code_path = os.path.join(run_dir, "exit_code")
    meta_path = os.path.join(run_dir, "meta.json")

    # Run through a shell wrapper so the exit code lands on disk even if this
    # MCP server process restarts before the child finishes -- status/result
    # lookups never depend on an in-memory subprocess.Popen handle.
    quoted_cmd = " ".join(_shell_quote(c) for c in cmd)
    wrapper = f"{quoted_cmd} > {_shell_quote(log_path)} 2>&1; echo $? > {_shell_quote(exit_code_path)}"

    try:
        proc = subprocess.Popen(["bash", "-c", wrapper], cwd=cwd, start_new_session=True)
    except FileNotFoundError:
        return _error_result("`bash` or `claude` not found on PATH.")

    with open(meta_path, "w") as f:
        json.dump({
            "run_id": run_id,
            "task": task,
            "allowed_tools": allowed_tools,
            "cwd": cwd,
            "wrapper_pid": proc.pid,
            "started_at": time.time(),
        }, f)

    return {
        "content": [{
            "type": "text",
            "text": f"Started local delegation. run_id: {run_id}\n"
                    f"Poll with check_delegate_status, then get_delegate_result once complete.",
        }],
        "isError": False,
    }


def _shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _load_meta(run_id):
    meta_path = os.path.join(_run_dir(run_id), "meta.json")
    if not os.path.isfile(meta_path):
        return None
    with open(meta_path) as f:
        return json.load(f)


def check_status(args):
    run_id = args.get("run_id")
    meta = _load_meta(run_id) if run_id else None
    if meta is None:
        return _error_result(f"Unknown run_id: {run_id}")

    run_dir = _run_dir(run_id)
    exit_code_path = os.path.join(run_dir, "exit_code")
    log_path = os.path.join(run_dir, "output.log")

    if os.path.isfile(exit_code_path):
        with open(exit_code_path) as f:
            code = f.read().strip()
        status = "completed" if code == "0" else "error"
        text = f"run_id {run_id}: {status} (exit code {code}). Call get_delegate_result for the answer."
        return {"content": [{"type": "text", "text": text}], "isError": False}

    running = True
    try:
        os.kill(meta["wrapper_pid"], 0)
    except (ProcessLookupError, PermissionError):
        running = False

    tail = ""
    if os.path.isfile(log_path):
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LOG_TAIL_CHARS))
            tail = f.read().decode("utf-8", errors="replace")

    elapsed = time.time() - meta.get("started_at", time.time())
    if running:
        status = "running"
    else:
        status = "unknown (process gone, no exit code written -- check log)"

    text = f"run_id {run_id}: {status}, {elapsed:.0f}s elapsed.\n\n--- log tail ---\n{tail}"
    return {"content": [{"type": "text", "text": text}], "isError": False}


def get_result(args):
    run_id = args.get("run_id")
    meta = _load_meta(run_id) if run_id else None
    if meta is None:
        return _error_result(f"Unknown run_id: {run_id}")

    run_dir = _run_dir(run_id)
    exit_code_path = os.path.join(run_dir, "exit_code")
    log_path = os.path.join(run_dir, "output.log")

    if not os.path.isfile(exit_code_path):
        return _error_result(f"run_id {run_id} is still running. Call check_delegate_status first.")

    with open(exit_code_path) as f:
        code = f.read().strip()

    with open(log_path) as f:
        raw = f.read()

    if code != "0":
        return _error_result(f"claude -p exited {code}. Log tail:\n{raw[-2000:]}")

    result_event = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            result_event = event

    if result_event is None:
        return _error_result(f"Could not find a result event in the output. Log tail:\n{raw[-2000:]}")

    result_text = result_event.get("result", "")
    session_id = result_event.get("session_id", "")
    cost = result_event.get("total_cost_usd")

    footer = f"\n\n-- local session_id: {session_id}"
    if cost is not None:
        footer += (
            f", est. cost: ${cost} "
            "(Claude Code's own client-side estimate at Anthropic API rates -- "
            "not a real charge against a free local model)"
        )
    return {"content": [{"type": "text", "text": result_text + footer}], "isError": False}


def _error_result(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


TOOL_HANDLERS = {
    "delegate_to_local": start_delegate,
    "check_delegate_status": check_status,
    "get_delegate_result": get_result,
}


def handle_request(msg):
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return _response(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "tools/list":
        return _response(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {})
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return _response(msg_id, None, error={"code": -32601, "message": f"Unknown tool: {name}"})
        return _response(msg_id, handler(args))

    if method in ("notifications/initialized", "notifications/cancelled"):
        return None  # notifications get no response

    if msg_id is not None:
        return _response(msg_id, None, error={"code": -32601, "message": f"Unknown method: {method}"})
    return None


def _response(msg_id, result, error=None):
    resp = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        resp["error"] = error
    else:
        resp["result"] = result
    return resp


def main():
    os.makedirs(RUNS_DIR, exist_ok=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
