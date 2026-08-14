#!/usr/bin/env python3
"""
MCP server exposing one tool, delegate_to_local, that spawns a real
`claude -p` headless subprocess pointed at a local model backend
(via --settings). The delegated call gets the actual Claude Code
agent loop (tools, context management, CLAUDE.md) -- not a hand-rolled
prompt wrapper -- just running against a local model instead of Anthropic's.

Stdlib-only: no `mcp` SDK dependency, so no venv/pip install is needed.
Implements the MCP stdio transport directly (newline-delimited JSON-RPC 2.0).
"""

import json
import os
import subprocess
import sys

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "claude-local-delegate"
SERVER_VERSION = "0.1.0"

DEFAULT_SETTINGS_PATH = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_SETTINGS",
    os.path.expanduser("~/.claude/vllm.settings.json"),
)
DEFAULT_ALLOWED_TOOLS = "Read,Grep,Glob"
DEFAULT_TIMEOUT_S = 300

TOOLS = [
    {
        "name": "delegate_to_local",
        "description": (
            "Delegate a self-contained task to the local model (served by vLLM) "
            "by spawning a real headless `claude -p` subprocess with --settings "
            "pointed at the local backend. The local model gets the full Claude "
            "Code agent loop (its own tool calls, its own context management) "
            "for this task, not a summarized prompt. Use for mechanical / "
            "high-volume work (drafts, boilerplate, straightforward refactors, "
            "summarization) where the extra local-model latency is worth saving "
            "tokens on the main session. Do not use for tasks needing careful "
            "judgment, architecture decisions, or security-sensitive changes -- "
            "keep those in the main session."
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
                "timeout_s": {
                    "type": "integer",
                    "description": f"Max seconds to wait for the local sub-session. Default {DEFAULT_TIMEOUT_S}.",
                },
                "resume_session_id": {
                    "type": "string",
                    "description": "Optional session_id from a prior delegate_to_local call, to continue that local sub-session instead of starting a fresh one.",
                },
            },
            "required": ["task"],
        },
    }
]


def run_delegate(args):
    task = args.get("task")
    if not task or not isinstance(task, str):
        return _error_result("`task` is required and must be a non-empty string.")

    allowed_tools = args.get("allowed_tools") or DEFAULT_ALLOWED_TOOLS
    cwd = args.get("cwd") or os.getcwd()
    timeout_s = args.get("timeout_s") or DEFAULT_TIMEOUT_S
    resume_id = args.get("resume_session_id")

    if not os.path.isfile(DEFAULT_SETTINGS_PATH):
        return _error_result(
            f"Local settings file not found: {DEFAULT_SETTINGS_PATH}. "
            "Set CLAUDE_LOCAL_DELEGATE_SETTINGS or create the file."
        )

    cmd = [
        "claude", "-p", task,
        "--settings", DEFAULT_SETTINGS_PATH,
        "--allowedTools", allowed_tools,
        "--output-format", "json",
    ]
    if resume_id:
        cmd += ["--resume", resume_id]

    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout_s,
        )
    except FileNotFoundError:
        return _error_result("`claude` binary not found on PATH.")
    except subprocess.TimeoutExpired:
        return _error_result(f"Local delegation timed out after {timeout_s}s.")

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip()[-2000:]
        return _error_result(f"claude -p exited {proc.returncode}: {stderr_tail}")

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return _error_result(f"Could not parse claude -p output as JSON: {proc.stdout[:2000]}")

    result_text = payload.get("result", "")
    session_id = payload.get("session_id", "")
    cost = payload.get("total_cost_usd")

    summary = result_text
    footer = f"\n\n-- local session_id: {session_id}"
    if cost is not None:
        footer += f", est. cost: ${cost}"
    return {"content": [{"type": "text", "text": summary + footer}], "isError": False}


def _error_result(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


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
        if name != "delegate_to_local":
            return _response(msg_id, None, error={"code": -32601, "message": f"Unknown tool: {name}"})
        return _response(msg_id, run_delegate(args))

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
