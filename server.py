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

Bi-directional communication: each delegated run is launched with its own
--mcp-config pointing back at THIS SAME SCRIPT, spawned as a second,
separate process with CLAUDE_LOCAL_DELEGATE_ROLE=child in its environment.
That child-mode process exposes only ask_parent/check_message_status (not
delegate_to_local -- no unbounded recursive delegation) and exchanges
messages with the parent-mode process via JSON files under the run's
messages/ directory. Parent and child are always different OS processes
(each `claude` session spawns its own stdio server) -- there is no shared
memory, only the filesystem.

Stdlib-only: no `mcp` SDK dependency, so no venv/pip install is needed.
Implements the MCP stdio transport directly (newline-delimited JSON-RPC 2.0).
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "claude-local-delegate"
SERVER_VERSION = "0.3.0"

SELF_PATH = os.path.abspath(__file__)
ROLE = os.environ.get("CLAUDE_LOCAL_DELEGATE_ROLE", "parent")
CHILD_RUN_ID = os.environ.get("CLAUDE_LOCAL_DELEGATE_RUN_ID")

DEFAULT_SETTINGS_PATH = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_SETTINGS",
    os.path.expanduser("~/.claude/vllm.settings.json"),
)
DEFAULT_ALLOWED_TOOLS = "Read,Grep,Glob"
RUNS_DIR = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_RUNS_DIR",
    os.path.expanduser("~/.claude-local-delegate/runs"),
)
BATCHES_DIR = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_BATCHES_DIR",
    os.path.expanduser("~/.claude-local-delegate/batches"),
)
# vLLM concurrency ceiling from this stack's docker-compose (--max-num-seqs).
# Not enforced here -- vLLM's own scheduler queues excess requests rather
# than failing -- but fan_out_to_local warns past this because many
# concurrent long-context runs contend for the same finite KV-cache/VRAM
# pool: throughput doesn't scale linearly once you're past what the box can
# actually hold resident, even though nothing errors out.
LOCAL_SERVER_MAX_CONCURRENCY = int(os.environ.get("CLAUDE_LOCAL_DELEGATE_MAX_CONCURRENCY", "16"))
ASK_PARENT_TOOL_NAME = "mcp__claude-local-delegate__ask_parent"
CHECK_MESSAGE_TOOL_NAME = "mcp__claude-local-delegate__check_message_status"
MESSAGE_POLL_TOTAL_S = 50
MESSAGE_POLL_INTERVAL_S = 2
# check_delegate_status/check_fanout_status long-poll for this long before
# returning a "still running" snapshot -- there is no push channel back to
# the calling session (this server only ever speaks when spoken to over
# stdio), so a status call that returns instantly forces the caller to guess
# when to check again. Blocking here means a single call has a real chance
# of landing on completion instead of a stale snapshot.
STATUS_POLL_TOTAL_S = 50
STATUS_POLL_INTERVAL_S = 2

ASK_PARENT_SYSTEM_NOTE = (
    "You have an ask_parent tool. If you are genuinely blocked -- something "
    "only the session that delegated this task to you can decide or clarify "
    "-- call ask_parent with a specific question, then poll "
    "check_message_status until it's answered. Don't use it for things you "
    "can reasonably decide yourself; try to make progress with your own "
    "judgment first."
)

PARENT_TOOLS = [
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
            "check_delegate_status also surfaces any pending question the "
            "delegated run has asked you via its own ask_parent tool -- answer "
            "those with reply_to_delegate. Use for mechanical / high-volume work "
            "(drafts, boilerplate, straightforward refactors, summarization) "
            "where local-model latency is worth saving tokens on the main "
            "session. Do not use for tasks needing careful judgment, "
            "architecture decisions, or security-sensitive changes -- keep "
            "those in the main session."
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
                        "model runs unsupervised with whatever access you grant here. "
                        "ask_parent/check_message_status are always available to the "
                        "delegated run regardless of this setting."
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
                "bare": {
                    "type": "boolean",
                    "description": (
                        "Default false: full context (skills, plugins, hooks, CLAUDE.md -- "
                        "same as an interactive session). Set true to run with --bare "
                        "instead, which cuts a large fixed per-call overhead (tens of "
                        "thousands of input tokens on this project's skill/plugin catalog "
                        "in testing) at the cost of dropping project-specific skills, "
                        "hooks, and plugins the task might actually need. CLAUDE.md is "
                        "always re-added even in bare mode, since project conventions "
                        "matter for quality. Only set true for tasks you're confident "
                        "don't depend on anything bare mode strips -- when unsure, leave "
                        "the default."
                    ),
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "check_delegate_status",
        "description": (
            "Check whether a delegate_to_local run (by run_id) is still running, "
            "completed, or failed. Blocks server-side for up to "
            f"{STATUS_POLL_TOTAL_S}s waiting for the run to finish (or ask a "
            "question) before returning a snapshot -- there is no separate "
            "completion notification, so call this again (it's fine to call it "
            "repeatedly back-to-back) until it reports completed/error rather "
            "than assuming a background push will tell you. Returns a tail of "
            "its log while running, plus any pending question the run has asked "
            "via ask_parent -- answer those with reply_to_delegate so the run "
            "can continue."
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
            "check_delegate_status first. IMPORTANT: review this output before "
            "treating it as final, especially anything that will be committed "
            "or run unattended (config values, weights/signs, file edits). A "
            "delegated run can produce plausible-looking but subtly wrong "
            "output -- e.g. correct-looking code with an inverted sign on a "
            "config parameter -- that only a read-through catches. Reviewing "
            "already-generated output is cheap relative to what delegation "
            "saved; don't skip it just because the local model reported success."
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
        "name": "reply_to_delegate",
        "description": (
            "Answer a pending question a delegated run asked via its own "
            "ask_parent tool (surfaced by check_delegate_status). The run's "
            "own check_message_status call picks up the answer and continues."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "run_id the question came from."},
                "message_id": {"type": "string", "description": "message_id from check_delegate_status's pending-question listing."},
                "answer": {"type": "string", "description": "Your answer to the delegated run's question."},
            },
            "required": ["run_id", "message_id", "answer"],
        },
    },
    {
        "name": "fan_out_to_local",
        "description": (
            "Map-reduce over the local model: split a large body of independent "
            "material (e.g. many doc pages, files, or search results) into "
            "`items` and process each in its own parallel delegate_to_local run "
            "with the same `shared_instruction`. Use this instead of stuffing "
            "everything into one huge local context -- each item still fits in "
            "the local model's own window (chunking also tends to beat one "
            "giant context on accuracy; long-context recall degrades the more "
            "you cram in, 'lost in the middle'), and this session only ever "
            "reads the final synthesized answer, not the raw material. Returns "
            "immediately with a batch_id; poll check_fanout_status, then "
            "get_fanout_result once every item is done.\n\n"
            "Real ceiling to know about: parallel runs share this box's fixed "
            f"vLLM concurrency ({LOCAL_SERVER_MAX_CONCURRENCY} concurrent "
            "sequences here) and, more importantly, its VRAM/KV-cache pool. "
            "More items than that don't fail, they queue -- and if each item's "
            "context is itself large, several of them near-simultaneously can "
            "contend for the same KV-cache, so parallelism doesn't scale as "
            "cleanly as spinning up more items always implies. Reasonable batch "
            f"sizes (up to roughly {LOCAL_SERVER_MAX_CONCURRENCY}) with "
            "moderate per-item context is the sweet spot, not 'as many as "
            "possible.'"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Independent pieces of material, one per parallel local run. Each becomes its own delegate_to_local task: shared_instruction + this item.",
                },
                "shared_instruction": {
                    "type": "string",
                    "description": "The instruction applied to every item (e.g. 'Summarize the key API changes in this doc page').",
                },
                "allowed_tools": {
                    "type": "string",
                    "description": f"Same as delegate_to_local's allowed_tools, applied to every item. Defaults to read-only ('{DEFAULT_ALLOWED_TOOLS}').",
                },
                "cwd": {"type": "string", "description": "Working directory for every item's local sub-session."},
                "bare": {"type": "boolean", "description": "Same as delegate_to_local's bare param, applied to every item."},
            },
            "required": ["items", "shared_instruction"],
        },
    },
    {
        "name": "check_fanout_status",
        "description": (
            "Check progress of a fan_out_to_local batch: how many items "
            f"completed/errored/still running. Blocks server-side for up to "
            f"{STATUS_POLL_TOTAL_S}s waiting for all items to finish before "
            "returning -- there is no separate completion notification, so "
            "call again if items are still running. Also warns if running "
            "items look stuck on permission denials."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string", "description": "batch_id returned by fan_out_to_local."},
            },
            "required": ["batch_id"],
        },
    },
    {
        "name": "get_fanout_result",
        "description": (
            "Fetch results from a fan_out_to_local batch. Errors if any item "
            "isn't done yet -- call check_fanout_status first. Without "
            "aggregate_instruction, returns all items' raw results concatenated "
            "(you review and synthesize yourself). With aggregate_instruction, "
            "starts ONE MORE local run that reads all the items' results and "
            "synthesizes per that instruction -- an ordinary delegate_to_local "
            "run under the hood, so poll it with the normal "
            "check_delegate_status/get_delegate_result (same review-before-"
            "trusting caveat applies to the synthesized answer)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string", "description": "batch_id returned by fan_out_to_local."},
                "aggregate_instruction": {
                    "type": "string",
                    "description": "Optional: if set, spawns a local run to synthesize all items' results per this instruction instead of returning them raw.",
                },
            },
            "required": ["batch_id"],
        },
    },
]

CHILD_TOOLS = [
    {
        "name": "ask_parent",
        "description": (
            "Ask the session that delegated this task to you a question, when "
            "you're genuinely blocked on something only it can decide or "
            "clarify. Returns a message_id -- poll check_message_status with it "
            "until you get an answer. Don't use this for things you can "
            "reasonably decide with your own judgment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Your specific question for the parent session."},
            },
            "required": ["question"],
        },
    },
    {
        "name": "check_message_status",
        "description": (
            "Poll for the answer to a question you asked via ask_parent. Blocks "
            f"server-side for up to {MESSAGE_POLL_TOTAL_S}s waiting for an "
            "answer before returning 'still pending' -- call again if you get "
            "that back and the question still matters."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "message_id returned by ask_parent."},
            },
            "required": ["message_id"],
        },
    },
]


def _find_claude_md(start_dir):
    """Walk upward from start_dir looking for CLAUDE.md, stopping at the
    nearest one found or at the filesystem/git root -- mirrors Claude Code's
    own project-root discovery closely enough for our purposes without
    needing to shell out to `git`."""
    d = os.path.abspath(start_dir)
    while True:
        candidate = os.path.join(d, "CLAUDE.md")
        if os.path.isfile(candidate):
            return candidate
        if os.path.isdir(os.path.join(d, ".git")):
            return None  # repo root reached, no CLAUDE.md in it
        parent = os.path.dirname(d)
        if parent == d:
            return None  # filesystem root reached
        d = parent


def _run_dir(run_id):
    return os.path.join(RUNS_DIR, run_id)


def _messages_dir(run_id):
    return os.path.join(_run_dir(run_id), "messages")


def _message_path(run_id, message_id):
    return os.path.join(_messages_dir(run_id), f"{message_id}.json")


def start_delegate(args):
    task = args.get("task")
    if not task or not isinstance(task, str):
        return _error_result("`task` is required and must be a non-empty string.")

    allowed_tools = args.get("allowed_tools") or DEFAULT_ALLOWED_TOOLS
    cwd = args.get("cwd") or os.getcwd()
    resume_id = args.get("resume_session_id")
    bare = bool(args.get("bare", False))

    run_id, err = _start_run(task, allowed_tools, cwd, bare, resume_id)
    if err:
        return _error_result(err)

    return {
        "content": [{
            "type": "text",
            "text": f"Started local delegation. run_id: {run_id}\n"
                    f"Poll with check_delegate_status, then get_delegate_result once complete.",
        }],
        "isError": False,
    }


def _start_run(task, allowed_tools, cwd, bare, resume_id=None):
    """Core of delegate_to_local, factored out so fan_out_to_local can spawn
    N of these (and the aggregation pass) without going through the
    tool-call wrapping. Returns (run_id, None) on success or (None, error_message)."""
    if not os.path.isfile(DEFAULT_SETTINGS_PATH):
        return None, (
            f"Local settings file not found: {DEFAULT_SETTINGS_PATH}. "
            "Set CLAUDE_LOCAL_DELEGATE_SETTINGS or create the file."
        )

    run_id = uuid.uuid4().hex[:12]
    run_dir = _run_dir(run_id)
    os.makedirs(_messages_dir(run_id), exist_ok=True)

    with open(os.path.join(run_dir, "prompt.md"), "w") as f:
        f.write(task)

    mcp_config_path = os.path.join(run_dir, "mcp-config.json")
    with open(mcp_config_path, "w") as f:
        json.dump({
            "mcpServers": {
                "claude-local-delegate": {
                    "command": sys.executable,
                    "args": [SELF_PATH],
                    "env": {
                        "CLAUDE_LOCAL_DELEGATE_ROLE": "child",
                        "CLAUDE_LOCAL_DELEGATE_RUN_ID": run_id,
                        "CLAUDE_LOCAL_DELEGATE_RUNS_DIR": RUNS_DIR,
                    },
                }
            }
        }, f)

    child_allowed_tools = f"{allowed_tools},{ASK_PARENT_TOOL_NAME},{CHECK_MESSAGE_TOOL_NAME}"

    # claude -p rejects --append-system-prompt and --append-system-prompt-file
    # together, so everything we want appended (the ask_parent note, and in
    # bare mode CLAUDE.md) goes into one file and one flag.
    system_prompt_addition = ASK_PARENT_SYSTEM_NOTE
    if bare:
        # --bare drops CLAUDE.md along with the (much larger) skills/plugins
        # catalog that's the actual point of --bare. Re-add just CLAUDE.md so
        # the delegated run still has project conventions -- looked up from
        # cwd upward, same order Claude Code's own discovery uses. Full
        # (non-bare) mode already gets CLAUDE.md the normal way.
        claude_md_path = _find_claude_md(cwd)
        if claude_md_path:
            with open(claude_md_path) as f:
                system_prompt_addition += "\n\n" + f.read()

    system_prompt_path = os.path.join(run_dir, "system-prompt-addition.md")
    with open(system_prompt_path, "w") as f:
        f.write(system_prompt_addition)

    cmd = [
        "claude", "-p", task,
        "--settings", DEFAULT_SETTINGS_PATH,
        "--allowedTools", child_allowed_tools,
        "--mcp-config", mcp_config_path,
        "--append-system-prompt-file", system_prompt_path,
        "--output-format", "stream-json",
        "--verbose",
    ]

    if bare:
        cmd.insert(1, "--bare")

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
        return None, "`bash` or `claude` not found on PATH."

    with open(meta_path, "w") as f:
        json.dump({
            "run_id": run_id,
            "task": task,
            "allowed_tools": allowed_tools,
            "cwd": cwd,
            "wrapper_pid": proc.pid,
            "started_at": time.time(),
        }, f)

    return run_id, None


def _shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _load_meta(run_id):
    meta_path = os.path.join(_run_dir(run_id), "meta.json")
    if not os.path.isfile(meta_path):
        return None
    with open(meta_path) as f:
        return json.load(f)


def _pending_messages(run_id):
    mdir = _messages_dir(run_id)
    if not os.path.isdir(mdir):
        return []
    pending = []
    for fname in os.listdir(mdir):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(mdir, fname)) as f:
            try:
                msg = json.load(f)
            except json.JSONDecodeError:
                continue
        if msg.get("status") == "pending":
            pending.append(msg)
    return pending


_BLOCKED_PATTERNS = (
    "haven't granted", "requires approval", "was blocked", "permission denied",
    "requires permission", "not been granted", "denied by the",
)


def _summarize_log(log_path, max_events=8):
    """Compact, human-readable progress summary from the run's stream-json
    log -- deliberately NOT a raw tail. A raw tail of this log is mostly
    system/init noise (the full skill/tool catalog dump, hundreds of tokens
    on its own) that costs the PARENT session real paid tokens for zero
    signal every time it polls. This extracts only what actually happened:
    assistant text, tool calls, tool results, each truncated.

    Also counts tool_results that look like permission/hook denials across
    the WHOLE log (not just the tail) -- a run repeatedly hitting the same
    wall is a distinct, actionable signal (probably needs wider
    allowed_tools) that's easy to miss buried in a long event list."""
    if not os.path.isfile(log_path):
        return "(no output yet)", 0

    events = []
    blocked_count = 0
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")
            if etype == "system":
                continue  # init dump -- pure noise for a progress summary
            if etype == "assistant":
                for item in event.get("message", {}).get("content", []):
                    if item.get("type") == "text" and item.get("text", "").strip():
                        events.append(f"[assistant] {item['text'].strip()[:200]}")
                    elif item.get("type") == "tool_use":
                        events.append(f"[tool_use] {item.get('name')}")
            elif etype == "user":
                for item in event.get("message", {}).get("content", []):
                    if item.get("type") == "tool_result":
                        content = item.get("content")
                        if isinstance(content, list):
                            text = " ".join(
                                c.get("text", "") for c in content if isinstance(c, dict)
                            )
                        else:
                            text = str(content)
                        text = text.strip()
                        lowered = text.lower()
                        if any(p in lowered for p in _BLOCKED_PATTERNS):
                            blocked_count += 1
                            events.append(f"[BLOCKED] {text[:150]}")
                        else:
                            events.append(f"[tool_result] {text[:150]}")
            elif etype == "result":
                events.append(f"[done] {event.get('result', '')[:200]}")

    if not events:
        return "(no output yet)", blocked_count
    return "\n".join(events[-max_events:]), blocked_count


def _estimate_running_tokens(log_path):
    """Best-effort running token/turn count while a run is still in progress
    -- summed from each assistant event's own usage field. Not authoritative
    (usage isn't always populated on every intermediate event the same way
    the final `result` event's totals are), but enough to catch a run that's
    quietly racked up an unexpectedly large number of turns/tokens before it
    finishes, since get_delegate_result only has numbers AFTER completion."""
    if not os.path.isfile(log_path):
        return 0, 0, 0

    turns = 0
    input_tokens = 0
    output_tokens = 0
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "assistant":
                continue
            turns += 1
            usage = event.get("message", {}).get("usage", {})
            input_tokens += usage.get("input_tokens", 0) or 0
            output_tokens += usage.get("output_tokens", 0) or 0
    return turns, input_tokens, output_tokens


def check_status(args):
    run_id = args.get("run_id")
    meta = _load_meta(run_id) if run_id else None
    if meta is None:
        return _error_result(f"Unknown run_id: {run_id}")

    run_dir = _run_dir(run_id)
    exit_code_path = os.path.join(run_dir, "exit_code")
    log_path = os.path.join(run_dir, "output.log")

    # Long-poll: block here (instead of returning an instant snapshot) so a
    # single call has a real chance to land on completion or a fresh
    # question. There's no way for this server to push a notification to
    # the calling session later -- it only runs while handling a request --
    # so this is the only way a status check can be more useful than a
    # coin-flip snapshot of a background run that takes minutes.
    deadline = time.time() + STATUS_POLL_TOTAL_S
    while True:
        if os.path.isfile(exit_code_path) or _pending_messages(run_id):
            break
        if time.time() >= deadline:
            break
        time.sleep(STATUS_POLL_INTERVAL_S)

    pending = _pending_messages(run_id)
    pending_block = ""
    if pending:
        lines = "\n".join(f"  - message_id {m['message_id']}: {m['question']}" for m in pending)
        pending_block = f"\n\nPENDING QUESTIONS from this run -- answer with reply_to_delegate:\n{lines}"

    if os.path.isfile(exit_code_path):
        with open(exit_code_path) as f:
            code = f.read().strip()
        status = "completed" if code == "0" else "error"
        text = (
            f"run_id {run_id}: {status} (exit code {code}). "
            f"Call get_delegate_result for the answer.{pending_block}"
        )
        return {"content": [{"type": "text", "text": text}], "isError": False}

    running = True
    try:
        os.kill(meta["wrapper_pid"], 0)
    except (ProcessLookupError, PermissionError):
        running = False

    summary, blocked_count = _summarize_log(log_path)
    turns, input_tok, output_tok = _estimate_running_tokens(log_path)

    elapsed = time.time() - meta.get("started_at", time.time())
    status = "running" if running else "unknown (process gone, no exit code written -- check log)"

    blocked_warning = ""
    if blocked_count >= 2:
        blocked_warning = (
            f"\n\n⚠ {blocked_count} tool calls in this run look like permission/hook "
            "denials -- it may be stuck retrying around something it doesn't have "
            "access to. Consider whether allowed_tools needs to be wider, or check "
            "the log tail below for what it's blocked on."
        )

    token_line = ""
    if turns:
        token_line = f" ~{turns} turns so far (best-effort estimate: {input_tok} input / {output_tok} output tokens)."

    text = (
        f"run_id {run_id}: {status}, {elapsed:.0f}s elapsed.{token_line}"
        f"{blocked_warning}{pending_block}\n\n--- progress ---\n{summary}"
    )
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _read_run_result(run_id):
    """Core of get_delegate_result, factored out so fan_out_to_local's
    aggregation step can pull each sub-run's answer directly. Returns a dict
    with result_text/session_id/cost/usage/duration_api_ms/error -- 'error'
    is set (and everything else None) if the run isn't done, failed, or its
    output couldn't be parsed."""
    meta = _load_meta(run_id) if run_id else None
    if meta is None:
        return {"error": f"Unknown run_id: {run_id}"}

    run_dir = _run_dir(run_id)
    exit_code_path = os.path.join(run_dir, "exit_code")
    log_path = os.path.join(run_dir, "output.log")

    if not os.path.isfile(exit_code_path):
        return {"error": f"run_id {run_id} is still running. Call check_delegate_status first."}

    with open(exit_code_path) as f:
        code = f.read().strip()

    with open(log_path) as f:
        raw = f.read()

    if code != "0":
        return {"error": f"claude -p exited {code}. Log tail:\n{raw[-2000:]}"}

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
        return {"error": f"Could not find a result event in the output. Log tail:\n{raw[-2000:]}"}

    return {
        "error": None,
        "result_text": _strip_think_blocks(result_event.get("result", "")),
        "session_id": result_event.get("session_id", ""),
        "cost": result_event.get("total_cost_usd"),
        "usage": result_event.get("usage", {}),
        "duration_api_ms": result_event.get("duration_api_ms"),
    }


def get_result(args):
    run_id = args.get("run_id")
    r = _read_run_result(run_id)
    if r["error"]:
        return _error_result(r["error"])

    footer = f"\n\n-- local session_id: {r['session_id']}"
    out_tok = r["usage"].get("output_tokens")
    if out_tok and r["duration_api_ms"]:
        tok_s = out_tok / (r["duration_api_ms"] / 1000)
        footer += f", {out_tok} output tokens in {r['duration_api_ms'] / 1000:.1f}s ({tok_s:.1f} tok/s)"
    if r["cost"] is not None:
        footer += (
            f", est. cost: ${r['cost']} "
            "(Claude Code's own client-side estimate at Anthropic API rates -- "
            "not a real charge against a free local model)"
        )
    return {"content": [{"type": "text", "text": r["result_text"] + footer}], "isError": False}


def _strip_think_blocks(text):
    """Defensive cleanup: some reasoning models leak <think>...</think> into
    their visible output if thinking isn't fully suppressed upstream (e.g. a
    --settings file pointed at a different local model than the one this was
    tuned against). Our own vllm.settings.json stack disables thinking at
    the chat-template level already, so this should normally be a no-op."""
    text = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    text = re.sub(r"^<think>.*", "", text, flags=re.DOTALL)  # orphaned opening tag
    return text


def reply_to_delegate(args):
    run_id = args.get("run_id")
    message_id = args.get("message_id")
    answer = args.get("answer")
    if not run_id or not message_id or answer is None:
        return _error_result("`run_id`, `message_id`, and `answer` are all required.")

    path = _message_path(run_id, message_id)
    if not os.path.isfile(path):
        return _error_result(f"No such message_id {message_id} for run_id {run_id}.")

    with open(path) as f:
        msg = json.load(f)
    msg["answer"] = answer
    msg["status"] = "answered"
    msg["answered_at"] = time.time()
    with open(path, "w") as f:
        json.dump(msg, f)

    return {"content": [{"type": "text", "text": f"Answer recorded for message_id {message_id}."}], "isError": False}


def _batch_path(batch_id):
    return os.path.join(BATCHES_DIR, f"{batch_id}.json")


def fan_out_to_local(args):
    items = args.get("items")
    shared_instruction = args.get("shared_instruction")
    if not items or not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        return _error_result("`items` is required and must be a non-empty array of strings.")
    if not shared_instruction or not isinstance(shared_instruction, str):
        return _error_result("`shared_instruction` is required and must be a non-empty string.")

    allowed_tools = args.get("allowed_tools") or DEFAULT_ALLOWED_TOOLS
    cwd = args.get("cwd") or os.getcwd()
    bare = bool(args.get("bare", False))

    run_ids = []
    for i, item in enumerate(items):
        task = f"{shared_instruction}\n\n--- item {i + 1}/{len(items)} ---\n\n{item}"
        run_id, err = _start_run(task, allowed_tools, cwd, bare)
        if err:
            # Best-effort: report what got started before the failure, plus the error.
            return _error_result(
                f"Failed to start item {i + 1}/{len(items)}: {err}\n"
                f"{len(run_ids)} item(s) already started: {run_ids}"
            )
        run_ids.append(run_id)

    batch_id = uuid.uuid4().hex[:12]
    os.makedirs(BATCHES_DIR, exist_ok=True)
    with open(_batch_path(batch_id), "w") as f:
        json.dump({
            "batch_id": batch_id,
            "run_ids": run_ids,
            "shared_instruction": shared_instruction,
            "allowed_tools": allowed_tools,
            "created_at": time.time(),
        }, f)

    warning = ""
    if len(items) > LOCAL_SERVER_MAX_CONCURRENCY:
        warning = (
            f"\n\nNote: {len(items)} items started, but the local server's configured "
            f"concurrency ceiling is {LOCAL_SERVER_MAX_CONCURRENCY} (vLLM --max-num-seqs). "
            "The extras will queue behind the first batch rather than run truly in "
            "parallel -- still correct, just not the full-parallel speedup you might expect."
        )

    return {
        "content": [{
            "type": "text",
            "text": f"Started {len(run_ids)} parallel local runs. batch_id: {batch_id}\n"
                    f"run_ids: {run_ids}\n"
                    f"Poll with check_fanout_status, then get_fanout_result once all are done.{warning}",
        }],
        "isError": False,
    }


def check_fanout_status(args):
    batch_id = args.get("batch_id")
    path = _batch_path(batch_id) if batch_id else None
    if not path or not os.path.isfile(path):
        return _error_result(f"Unknown batch_id: {batch_id}")
    with open(path) as f:
        batch = json.load(f)

    def _still_running(run_id):
        return not os.path.isfile(os.path.join(_run_dir(run_id), "exit_code"))

    # Long-poll like check_delegate_status: block until every item is done
    # or the deadline passes, rather than returning an instant "still
    # running" snapshot the caller has no reliable way to know to recheck.
    deadline = time.time() + STATUS_POLL_TOTAL_S
    while any(_still_running(run_id) for run_id in batch["run_ids"]) and time.time() < deadline:
        time.sleep(STATUS_POLL_INTERVAL_S)

    completed, errored, running, total_blocked = 0, 0, 0, 0
    for run_id in batch["run_ids"]:
        run_dir = _run_dir(run_id)
        exit_code_path = os.path.join(run_dir, "exit_code")
        if os.path.isfile(exit_code_path):
            with open(exit_code_path) as f:
                code = f.read().strip()
            if code == "0":
                completed += 1
            else:
                errored += 1
        else:
            running += 1
            _, blocked = _summarize_log(os.path.join(run_dir, "output.log"))
            total_blocked += blocked

    blocked_warning = ""
    if total_blocked >= 2:
        blocked_warning = (
            f"\n\n⚠ {total_blocked} denial-shaped tool call(s) across still-running items in this "
            "batch -- some may be stuck on missing allowed_tools access, same as a single run."
        )

    text = (
        f"batch_id {batch_id}: {completed} completed, {errored} errored, {running} still running "
        f"(of {len(batch['run_ids'])} total).{blocked_warning}\n"
        f"run_ids: {batch['run_ids']}"
    )
    return {"content": [{"type": "text", "text": text}], "isError": False}


def get_fanout_result(args):
    batch_id = args.get("batch_id")
    aggregate_instruction = args.get("aggregate_instruction")
    path = _batch_path(batch_id) if batch_id else None
    if not path or not os.path.isfile(path):
        return _error_result(f"Unknown batch_id: {batch_id}")
    with open(path) as f:
        batch = json.load(f)

    results = []
    for run_id in batch["run_ids"]:
        r = _read_run_result(run_id)
        if r["error"]:
            return _error_result(
                f"run_id {run_id} isn't ready or failed: {r['error']}\n"
                "Call check_fanout_status first -- all items must finish before fetching the batch result."
            )
        results.append(r["result_text"])

    if not aggregate_instruction:
        parts = [f"--- item {i + 1}/{len(results)} ---\n{text}" for i, text in enumerate(results)]
        return {"content": [{"type": "text", "text": "\n\n".join(parts)}], "isError": False}

    # Aggregation is just one more ordinary delegated run over the collected
    # results -- reuses every existing mechanism (async, ask_parent, review
    # reminder) instead of inventing a separate synthesis code path.
    combined = "\n\n".join(f"--- item {i + 1}/{len(results)} ---\n{text}" for i, text in enumerate(results))
    agg_task = f"{aggregate_instruction}\n\n{combined}"
    agg_run_id, err = _start_run(agg_task, DEFAULT_ALLOWED_TOOLS, os.getcwd(), False)
    if err:
        return _error_result(f"Failed to start aggregation run: {err}")

    return {
        "content": [{
            "type": "text",
            "text": f"All {len(results)} items done; started aggregation run. run_id: {agg_run_id}\n"
                    f"Poll with check_delegate_status, then get_delegate_result for the synthesized answer.",
        }],
        "isError": False,
    }


# ---- child-mode tools (only exposed when CLAUDE_LOCAL_DELEGATE_ROLE=child) ----

def ask_parent(args):
    if not CHILD_RUN_ID:
        return _error_result("ask_parent is only available inside a delegate_to_local run.")
    question = args.get("question")
    if not question or not isinstance(question, str):
        return _error_result("`question` is required and must be a non-empty string.")

    message_id = uuid.uuid4().hex[:12]
    os.makedirs(_messages_dir(CHILD_RUN_ID), exist_ok=True)
    with open(_message_path(CHILD_RUN_ID, message_id), "w") as f:
        json.dump({
            "message_id": message_id,
            "question": question,
            "answer": None,
            "status": "pending",
            "asked_at": time.time(),
        }, f)

    return {
        "content": [{
            "type": "text",
            "text": f"Question sent to parent session. message_id: {message_id}\n"
                    f"Poll check_message_status with this message_id for the answer.",
        }],
        "isError": False,
    }


def check_message_status(args):
    if not CHILD_RUN_ID:
        return _error_result("check_message_status is only available inside a delegate_to_local run.")
    message_id = args.get("message_id")
    if not message_id:
        return _error_result("`message_id` is required.")

    path = _message_path(CHILD_RUN_ID, message_id)
    if not os.path.isfile(path):
        return _error_result(f"No such message_id: {message_id}")

    deadline = time.time() + MESSAGE_POLL_TOTAL_S
    while time.time() < deadline:
        with open(path) as f:
            msg = json.load(f)
        if msg.get("status") == "answered":
            return {"content": [{"type": "text", "text": msg["answer"]}], "isError": False}
        time.sleep(MESSAGE_POLL_INTERVAL_S)

    return {
        "content": [{"type": "text", "text": "Still pending -- no answer yet. Call check_message_status again if this still matters."}],
        "isError": False,
    }


def _error_result(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


PARENT_HANDLERS = {
    "delegate_to_local": start_delegate,
    "check_delegate_status": check_status,
    "get_delegate_result": get_result,
    "reply_to_delegate": reply_to_delegate,
    "fan_out_to_local": fan_out_to_local,
    "check_fanout_status": check_fanout_status,
    "get_fanout_result": get_fanout_result,
}
CHILD_HANDLERS = {
    "ask_parent": ask_parent,
    "check_message_status": check_message_status,
}

TOOLS = CHILD_TOOLS if ROLE == "child" else PARENT_TOOLS
TOOL_HANDLERS = CHILD_HANDLERS if ROLE == "child" else PARENT_HANDLERS


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
