#!/usr/bin/env python3
"""
MCP server for delegating tasks to a local model, but WITHOUT inventing its own
agent abstraction. Every delegated task becomes a real NATIVE Claude Code
background agent (`claude --bg`) pointed at the local backend via --settings
(vLLM/LiteLLM). The delegated agent is therefore a genuine, first-class Claude
Code session that:

  * shows up in `claude agents` (the native agent-view panel),
  * can be inspected with `claude logs <id>` / `claude attach <id>`,
  * is reachable by the parent session through the NATIVE cross-session
    messaging tools (ListAgents / SendMessage) -- the agent's own `blocked`
    state is the native "I need input" signal, so this server no longer ships
    a bespoke ask_parent/check_message_status protocol.

This server is deliberately thin: it is the *spawner and observer* of native
agents, not a replacement for them. The heavy lifting (the agent loop, tools,
context management, the parent<->agent conversation) is all done by Claude
Code itself. That is the whole point -- the local model rides on the SAME
native machinery your main session already uses, so there is no divergent
"delegation entity" to maintain.

Parent-side tools:
  delegate_to_local      spawn one native `claude --bg` agent on the local model
  check_delegate_status  read its NATIVE state (+ surface its `blocked` question)
  get_delegate_result    read its NATIVE transcript (last assistant answer)
  fan_out_to_local       spawn N parallel native agents (map)
  check_fanout_status    aggregate their native states (reduce)
  get_fanout_result      aggregate their native transcripts (reduce)

Replying to a delegated agent is the NATIVE `SendMessage(<agent>)` tool on the
parent side (the agent's own ListAgents sees it), not an MCP call -- that is
what "working on the agents and their requests, natively" means here.

RECOVERING A DRIFTING AGENT (tested behaviour, read before you try to steer one):
a local `claude --bg` agent does NOT read a mid-run SendMessage -- it finishes
its current run first, and a fast local model usually finishes before the
message is ever looked at; once it settles to `done` it is no longer reachable
by SendMessage at all. So SendMessage is reliable in exactly ONE case: the
agent is `blocked` (state == blocked), i.e. it stopped and asked its own
question -- answer that with SendMessage. (If the parent session runs in a
different permission-mode class than the delegate -- e.g. parent on `auto`,
delegate on the default `bypassPermissions` -- that one SendMessage is held
for the user to approve once; approve it. Everything else here, watch_delegate
/ stop_delegate / delegate_to_local, is MCP-side and never gated.)

For everything else -- the agent is running and going the wrong way -- the loop
that actually works is:
  1. watch_delegate  -- confirm from the narration that it is drifting
  2. stop_delegate   -- SIGINT; it settles to `done` in ~10-15s, before its
                        next step
  3. delegate_to_local  -- re-delegate a smaller, sharper task. Nothing is
                        lost: the stopped run's transcript stays readable with
                        get_delegate_result, so fold anything useful it already
                        produced into the new task text.
Do NOT sit in a watch->SendMessage->watch loop hoping a running agent picks up
a correction; it will not.

Stdlib-only. Implements the MCP stdio transport directly (newline-delimited
JSON-RPC 2.0), same as before.
"""

import hashlib
import json
import os
import glob
import re
import signal
import subprocess
import sys
import time
import uuid

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "claude-local-delegate"
SERVER_VERSION = "0.6.0"

# Where the local backend profile lives (ANTHROPIC_BASE_URL -> vLLM/LiteLLM,
# model env, etc). This is what the spawned `claude --bg` agents use, so they
# run on the local model while the parent session keeps its own provider.
#
# Default is the SLIM delegate profile (vllm.delegate.settings.json), not the
# full vllm.settings.json: the full one enables the interactive plugins
# (everything-claude-code, context7), whose gateguard Fact-Forcing PreToolUse
# hook is built for a human-in-the-loop session (deny first Write/Bash, demand
# "present facts then retry"). An unattended local-model agent runs that loop
# poorly and parks. The slim profile keeps the same vLLM routing but drops the
# plugins, which also removes the large fixed skill/plugin-token overhead and
# the "MCP server needs authentication" friction. Fall back to the full profile
# if the slim one isn't present.
SLIM_SETTINGS_PATH = os.path.expanduser("~/.claude/vllm.delegate.settings.json")
FALLBACK_SETTINGS_PATH = os.path.expanduser("~/.claude/vllm.settings.json")


def _default_settings_path():
    """Resolve the settings profile lazily (at spawn time, not import time):
    this server is a long-lived process, so a slim profile created mid-session
    should be picked up without a restart."""
    override = os.environ.get("CLAUDE_LOCAL_DELEGATE_SETTINGS")
    if override:
        return override
    return SLIM_SETTINGS_PATH if os.path.isfile(SLIM_SETTINGS_PATH) else FALLBACK_SETTINGS_PATH
DEFAULT_ALLOWED_TOOLS = "Read,Grep,Glob"

# Bookkeeping for fan_out batches ONLY -- a map of batch_id -> [agent ids].
# This is not an "agent entity": the agents themselves are 100% native
# `claude --bg` sessions; this file just remembers which ones belong to a
# batch so check_fanout_status/get_fanout_result can aggregate them.
BATCHES_DIR = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_BATCHES_DIR",
    os.path.expanduser("~/.claude-local-delegate/batches"),
)
# vLLM concurrency ceiling from this stack (--max-num-seqs). Not enforced here;
# fan_out_to_local only warns past it (see README).
LOCAL_SERVER_MAX_CONCURRENCY = int(os.environ.get("CLAUDE_LOCAL_DELEGATE_MAX_CONCURRENCY", "16"))

# ---- verified-delegation loop ----------------------------------------------
# delegate_verified runs a CLOSED work->check->revise loop entirely on the local
# model, so the PARENT session only ever sees an answer a local checker has
# already signed off on. It is a lazily-advanced state machine (like fan_out's
# file-backed batches): the parent just polls check_verified_status, and each
# poll moves the run forward one step -- nothing blocks the single-threaded MCP
# stdio loop. State lives in ~/.claude-local-delegate/verified/<vid>.json.
VERIFIED_DIR = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_VERIFIED_DIR",
    os.path.expanduser("~/.claude-local-delegate/verified"),
)
DEFAULT_MAX_VERIFY_ITERS = int(os.environ.get("CLAUDE_LOCAL_DELEGATE_MAX_VERIFY_ITERS", "3"))
# Wall-clock ceiling for the WHOLE loop (all iterations). A local worker+checker
# round is slow; 3 rounds can legitimately take a while. Past this the run is
# force-failed so it can never spin unattended forever.
DEFAULT_VERIFY_TIMEOUT = int(os.environ.get("CLAUDE_LOCAL_DELEGATE_VERIFY_TIMEOUT", "5400"))
# Persona for the checker agent (adversarial: re-verifies independently, runs the
# build/tests, fixes NOTHING, emits a `VERDICT: PASS|FAIL` line). Falls back to
# no persona if the file is absent, same graceful-degrade rule as _default_agent.
CHECKER_PERSONA = os.environ.get("CLAUDE_LOCAL_DELEGATE_CHECKER_AGENT", "local-checker")
# The worker in a verify loop must be able to change code; the read-only default
# would make every check fail. Callers can still narrow this per run.
DEFAULT_VERIFY_WORKER_TOOLS = "Read,Grep,Glob,Edit,Write,Bash"
# The checker must inspect + run things but must NOT edit -- no Edit/Write here,
# by design, so a "fix" can only come from a fresh worker round.
VERIFY_CHECKER_TOOLS = "Read,Grep,Glob,Bash"

CLAUDE_BIN = os.environ.get("CLAUDE_LOCAL_DELEGATE_BIN", "claude")
CLAUDE_CONFIG_DIR = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_CONFIG_DIR", os.path.expanduser("~/.claude")
)
# MCP servers are NOT passed per-spawn: a fresh `claude --bg "task"` dispatch
# ignores --mcp-config/--strict-mcp-config (verified by probe). Instead the
# web-capability MCPs live at USER SCOPE (~/.claude.json: ParallelSearch,
# context7) and every delegated agent loads them automatically. The spawner
# itself (claude-local-delegate) is also user-scope, so a recursion guard
# (--disallowedTools mcp__claude-local-delegate) is added at spawn time -- see
# _spawn_native_agent. That is the native analogue of the old ROLE=child switch.
# Native `claude --bg` starts in manual mode, where --allowedTools does NOT
# auto-approve (unlike headless `claude -p`): the agent blocks on its first
# gated tool with no human present. So the spawner must set a --permission-mode
# for the granted tools to actually run unattended.
#
# Default is bypassPermissions: a delegated agent is UNATTENDED by design, so
# any mode that still prompts (acceptEdits leaves Bash gated; default prompts
# on everything) will park the agent forever on its first Bash call. With
# bypassPermissions the agent runs its full granted toolset (Bash included)
# without approval gates -- the same contract the old `claude -p` +
# --allowedTools had, just expressed with the native flag. This is the user's
# own local model on their own machine; narrow per-delegation with the
# permission_mode/disallowed_tools params or the env override when a task
# should not have shell.
DEFAULT_PERMISSION_MODE = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_PERMISSION_MODE", "bypassPermissions"
)

# Prepended to every delegated task (unless announce_plan=False). It turns the
# agent into something a parent session can SUPERVISE cheaply: the parent reads
# only the agent's plain-text messages (watch_delegate strips tool output), so
# the agent is told to narrate intent in plain sentences, keep steps small, and
# treat a mid-run message as a course-correction. The marker line lets
# watch_delegate show the real task without this boilerplate.
SUPERVISION_MARKER = "=== YOUR TASK (everything below is the task) ==="
SUPERVISED_PREAMBLE = (
    "SUPERVISED RUN. A parent session is watching you. It sees ONLY your "
    "plain-text messages -- never your tool calls or their output. So:\n"
    "  1. FIRST, before any tool call, post a short numbered plan (one line per step).\n"
    "  2. Before each step: one plain sentence saying what you are about to do.\n"
    "  3. After each step: one plain sentence on the outcome -- NOT a code or output dump.\n"
    "  4. Keep messages terse: no pasted code, no file contents, no big tables.\n"
    "  5. If a new instruction arrives mid-run, it is a course-correction from the "
    "supervisor: acknowledge it in one line and change course immediately.\n"
    "  6. Prefer finishing a small task over expanding scope. If the task is bigger "
    "than ~5 steps, say so in your plan instead of silently doing all of it.\n"
    "  7. Work IN PLACE at the exact paths you are given. Do NOT create or enter a "
    "git worktree, do NOT call EnterWorktree, do NOT branch -- if a Write or Edit "
    "fails, report the real error in one sentence and stop; never 'work around' it "
    "by relocating the work.\n\n"
    + SUPERVISION_MARKER + "\n"
)
DEFAULT_ANNOUNCE_PLAN = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_ANNOUNCE_PLAN", "1"
).strip().lower() not in ("0", "false", "no", "")


# ---- native agent spawning ---------------------------------------------------

def _slug_from_task(task, max_words=6):
    """A short, safe display name for the background agent (so `claude agents`
    shows a label, not the whole prompt)."""
    words = [w for w in task.replace("\n", " ").split() if w.strip()]
    slug = " ".join(words[:max_words]).strip()
    return (slug or "delegate").replace("'", "")


def _default_agent():
    """The worker persona applied to every delegated agent (its body is the
    agent's system prompt). local-worker disciplines a weaker local model:
    verify by running a check, return evidence not 'done', state assumptions.
    Resolution: explicit CLAUDE_LOCAL_DELEGATE_AGENT override, else the
    default persona if its file exists, else None (spawn with no --agent, so
    deleting the persona file degrades gracefully instead of breaking)."""
    override = os.environ.get("CLAUDE_LOCAL_DELEGATE_AGENT")
    if override:
        return override
    default = os.path.expanduser("~/.claude/agents/local-worker.md")
    return "local-worker" if os.path.isfile(default) else None


def _spawn_native_agent(task, allowed_tools, cwd, name, permission_mode=None,
                        disallowed_tools=None, agent=None, announce_plan=None):
    """Spawn ONE native `claude --bg` agent on the local model.

    Returns (short_id, None) on success or (None, error_message).
    The agent's working directory is `cwd` (the --bg session runs in the
    shell's cwd, exactly as `claude agents --json` reports it). --settings
    carries the vLLM profile through to the backgrounded session, which is the
    documented way to point dispatched sessions at a different gateway.

    permission_mode defaults to DEFAULT_PERMISSION_MODE (bypassPermissions)
    so the granted tools (Bash included) actually run unattended (see the note
    above the constant).

    disallowed_tools (comma-separated, e.g. "Bash") is passed as --disallowedTools
    to strip tools from the agent. Useful against user-level hooks/gates: a
    delegated agent that would reach for Bash can be forced to a Write-only
    path by disallowing Bash, sidestepping any Bash PreToolUse gate it can't
    satisfy on its own.

    agent (a subagent-definition name) is passed as --agent, applying that
    persona's system prompt/body to the delegated session. None -> no --agent.

    MCP servers (web capability) come from user scope, not per-spawn flags --
    see the note above DEFAULT_PERMISSION_MODE and the recursion guard below.
    """
    settings_path = _default_settings_path()
    if not os.path.isfile(settings_path):
        return None, (
            f"Local settings file not found: {settings_path}. "
            "Set CLAUDE_LOCAL_DELEGATE_SETTINGS or create the file."
        )

    tools = [t.strip() for t in (allowed_tools or DEFAULT_ALLOWED_TOOLS).split(",") if t.strip()]
    disallowed = [t.strip() for t in (disallowed_tools or "").split(",") if t.strip()]
    # Recursion guard (native analogue of the old ROLE=child): a delegated agent
    # is unattended, so strip the spawner MCP itself. It is registered at
    # USER SCOPE (so every fresh `claude --bg` loads it), which means without
    # this guard a delegated agent could call delegate_to_local and spawn further
    # delegations unboundedly. Disallow the whole claude-local-delegate server.
    if "mcp__claude-local-delegate" not in disallowed:
        disallowed.append("mcp__claude-local-delegate")
    pmode = permission_mode or DEFAULT_PERMISSION_MODE
    # agent=None means "use the default persona"; agent="" means "no persona".
    pagent = _default_agent() if agent is None else agent
    # Wrap the task so the delegated agent narrates its plan/steps in plain text
    # (what watch_delegate surfaces to the parent). announce_plan=None -> default.
    want_plan = DEFAULT_ANNOUNCE_PLAN if announce_plan is None else bool(announce_plan)
    effective_task = (SUPERVISED_PREAMBLE + task) if want_plan else task

    cmd = [
        CLAUDE_BIN, "--bg",
        "--name", name,
        "--settings", settings_path,
        "--permission-mode", pmode,
    ]
    if pagent:
        cmd += ["--agent", pagent]
    # MCP servers (ParallelSearch, context7, ...) come from USER SCOPE
    # (~/.claude.json) -- that is the scope a fresh `claude --bg "task"` dispatch
    # actually loads. Verified: --mcp-config/--strict-mcp-config are IGNORED on a
    # fresh --bg (a probe with them got zero MCP tools; one without them, relying
    # on user scope, called mcp__ParallelSearch__web_search successfully). So no
    # --mcp-config is passed here.
    # The task is the positional prompt. It MUST come BEFORE the variadic
    # --allowedTools/--disallowedTools flags, which greedily consume all trailing
    # arguments: `--allowedTools Read Write <task>` would swallow the task into
    # the tool list and the agent would start with no prompt (observed: blocked,
    # empty transcript). Put the task first, then the variadic flags.
    cmd.append(effective_task)
    if tools:
        cmd += ["--allowedTools", *tools]
    if disallowed:
        cmd += ["--disallowedTools", *disallowed]

    try:
        proc = subprocess.run(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120,
        )
    except FileNotFoundError:
        return None, f"`{CLAUDE_BIN}` not found on PATH."
    except subprocess.TimeoutExpired:
        return None, "Timed out spawning the background agent (claude --bg hung)."

    out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
    if proc.returncode != 0 and "backgrounded" not in out:
        return None, f"claude --bg exited {proc.returncode}.\n{out.strip()[-1500:]}"

    short_id = _parse_bg_id(out)
    if not short_id:
        return None, (
            "Spawned but could not parse the agent id from "
            f"claude --bg output. Raw output:\n{out.strip()[-800:]}"
        )
    return short_id, None


def _parse_bg_id(bg_output):
    """`claude --bg` prints e.g. `backgrounded · aa46976f`. Extract that id.

    Native agent ids are 8 hex chars. Guard against a LONGER hex-ish token (e.g.
    a 12-hex run id echoed back inside the agent's --name) by capping length at
    8, and by only scanning the part of the line AFTER the
    `backgrounded`/`started` keyword -- the real id always follows it."""
    def hexish(t):
        return 6 <= len(t) <= 8 and all(c in "0123456789abcdef" for c in t)

    for line in bg_output.splitlines():
        low = line.lower()
        kw = low.find("backgrounded")
        if kw < 0:
            kw = low.find("started")
        if kw < 0:
            continue
        for tok in line[kw:].replace("·", " ").split():
            t = tok.strip()
            if hexish(t):
                return t
    # Fallback: prefer an exact 8-hex token, else any bare 6-8 hex run.
    m = re.search(r"\b[0-9a-f]{8}\b", bg_output) or re.search(r"\b[0-9a-f]{6,8}\b", bg_output)
    return m.group(0) if m else None


# ---- native agent introspection (state + transcript) -------------------------

def _agents_json(include_completed=True):
    """Run `claude agents [--all] --json` and return the list of agent dicts.
    Cheap: it does not touch the model -- it reads the supervisor's roster."""
    cmd = [CLAUDE_BIN, "agents", "--json"] + (["--all"] if include_completed else [])
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout.decode("utf-8", "replace")
    start = out.find("[")
    if start < 0:
        return None
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError:
        return None


def _resolve_agent(handle, include_completed=True):
    """Find an agent by short id OR full session id. Returns (dict|None, err|None).
    `handle` is whatever delegate_to_local handed back."""
    agents = _agents_json(include_completed)
    if agents is None:
        return None, "Could not run `claude agents --json` (is the supervisor up?)"
    for a in agents:
        if a.get("id") == handle or str(a.get("sessionId", "")) == handle:
            return a, None
        # short-id prefix match against the full sessionId
        if a.get("sessionId", "").startswith(handle):
            return a, None
    return None, None


def _find_transcript(session_id):
    """Locate the native JSONL transcript for a session across all project dirs.
    Path is normally ~/.claude/projects/<sanitized-cwd>/<sessionId>.jsonl; we
    glob because the sanitized-cwd prefix depends on the agent's working dir."""
    pattern = os.path.join(glob.escape(CLAUDE_CONFIG_DIR), "projects", "*", f"{session_id}.jsonl")
    hits = glob.glob(pattern)
    if hits:
        return sorted(hits, key=os.path.getmtime, reverse=True)[0]
    return None


def _iter_events(transcript_path):
    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _last_assistant_text(transcript_path):
    """The delegated agent's final answer = its last non-empty assistant text.
    This is the native result; no separate result file to maintain."""
    last = None
    for e in _iter_events(transcript_path):
        if e.get("type") == "assistant":
            content = e.get("message", {}).get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and item.get("text", "").strip():
                    last = item["text"].strip()
    return last


def _first_user_text(transcript_path):
    """The original prompt the agent was spawned with = the first user event that
    carries real text (later user events are tool_results). Used by watch_delegate
    so the parent can see WHAT the agent was told without re-reading anything."""
    for e in _iter_events(transcript_path):
        if e.get("type") != "user":
            continue
        content = e.get("message", {}).get("content", [])
        if isinstance(content, str):
            if content.strip():
                return content.strip()
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text", "").strip():
                return item["text"].strip()
    return None


def _strip_preamble(prompt):
    """Drop the SUPERVISED_PREAMBLE boilerplate so watch_delegate shows the task."""
    if prompt and SUPERVISION_MARKER in prompt:
        return prompt.split(SUPERVISION_MARKER, 1)[1].strip()
    return prompt


def _narration_digest(transcript_path, max_lines=48):
    """Token-cheap supervision view: the agent's assistant plain-text messages in
    order, each tool_use collapsed to a ONE-LINE label, and tool RESULTS dropped
    entirely. This is what a human watching the agent think would see, minus the
    code / file dumps -- the whole point of 'read its messages, not its output'.
    max_lines <= 0 returns everything."""
    lines = []
    step = 0
    for e in _iter_events(transcript_path):
        if e.get("type") != "assistant":
            continue
        content = e.get("message", {}).get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text", "").strip():
                step += 1
                lines.append(f"[{step}] {item['text'].strip()}")
            elif item.get("type") == "tool_use":
                inp = json.dumps(item.get("input", {}), ensure_ascii=False)
                if len(inp) > 140:
                    inp = inp[:140] + "…"
                lines.append(f"      · {item.get('name')} {inp}")
    if max_lines and max_lines > 0:
        return lines[-max_lines:]
    return lines


def _token_usage(transcript_path):
    """(input_plus_cache, output, model_turns) summed from the transcript's
    assistant usage blocks. Directly answers 'how many tokens is this session
    generating' -- the number the sessions-vs-throughput tuning needs."""
    inp = out = turns = 0
    for e in _iter_events(transcript_path):
        if e.get("type") != "assistant":
            continue
        u = e.get("message", {}).get("usage", {}) or {}
        inp += ((u.get("input_tokens") or 0)
                + (u.get("cache_read_input_tokens") or 0)
                + (u.get("cache_creation_input_tokens") or 0))
        out += u.get("output_tokens") or 0
        turns += 1
    return inp, out, turns


def _tail_transcript(transcript_path, max_events=24):
    """The last few assistant texts / tool names / tool_result snippets, as a flat
    list of strings. Used to classify WHY an agent is blocked (the tail holds the
    gate message or the missing-tool report). Cheap: it never touches the model."""
    tail = []
    for e in _iter_events(transcript_path):
        t = e.get("type")
        if t == "assistant":
            content = e.get("message", {}).get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and item.get("text", "").strip():
                    tail.append(("text", item["text"].strip()))
                elif item.get("type") == "tool_use":
                    tail.append(("tool", f"{item.get('name')} {json.dumps(item.get('input', {}))[:200]}"))
        elif t == "user":
            content = e.get("message", {}).get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    raw = item.get("content")
                    if isinstance(raw, list):
                        raw = " ".join(c.get("text", "") for c in raw if isinstance(c, dict))
                    tail.append(("result", str(raw).strip()))
    return tail[-max_events:]


# Category heuristics for WHY an agent is blocked. Order matters: the most
# specific / actionable signals are checked first. Each returns a short label +
# a one-line "what to do" so the parent (main model) can act without re-reading
# the whole transcript.
_BLOCK_SIGNATURES = (
    ("hook-gate", ("Fact-Forcing Gate", "present these facts", "GateGuard",
                   "Before creating", "Before editing", "destructive command")),
    ("mcp-tool-missing", ("NO_MCP", "not available to you", "not present in your toolset",
                          "No such tool available", "mcp__.* not")),
    ("websearch-broken", ("tool_choice", "tools must be set", "VLLMValidation", "litellm.BadRequest")),
    ("permission-prompt", ("permission prompt", "requires approval", "needs permission",
                           "waiting for permission", "Permission for this action")),
    ("needs-input", ("what would you like", "should i", "do you want", "how would you like",
                     "which option", "let me know")),
)


def _classify_blocked(agent, transcript_path):
    """Return (category, last_words) describing why a `blocked` agent is parked.
    Preference: the native roster `waitingFor` field (Claude Code's own call) is
    the anchor; the transcript tail refines it to an actionable category."""
    waiting_for = (agent.get("waitingFor") or "").strip()
    wf = "permission-prompt" if "permission" in waiting_for.lower() else (
        "input-needed" if waiting_for else "unknown")

    category = wf
    last_words = ""
    if transcript_path:
        tail = _tail_transcript(transcript_path)
        for kind, val in tail:
            if kind == "text":
                last_words = val
        tail_text = " ".join(v for k, v in tail).lower()
        for label, pats in _BLOCK_SIGNATURES:
            for p in pats:
                if re.search(p, tail_text, flags=re.IGNORECASE):
                    category = label
                    break
            if category != wf:
                break
    return category, waiting_for, last_words


# ---- tool handlers ------------------------------------------------------------

def start_delegate(args):
    task = args.get("task")
    if not task or not isinstance(task, str):
        return _error_result("`task` is required and must be a non-empty string.")

    allowed_tools = args.get("allowed_tools") or DEFAULT_ALLOWED_TOOLS
    cwd = args.get("cwd") or os.getcwd()
    name = args.get("name") or _slug_from_task(task)
    permission_mode = args.get("permission_mode")
    disallowed_tools = args.get("disallowed_tools")
    agent = args.get("agent")  # None -> default persona; "" -> no persona; name -> that persona
    announce_plan = args.get("announce_plan")  # None -> DEFAULT_ANNOUNCE_PLAN

    short_id, err = _spawn_native_agent(
        task, allowed_tools, cwd, name, permission_mode, disallowed_tools, agent,
        announce_plan,
    )
    if err:
        return _error_result(err)

    return {
        "content": [{
            "type": "text",
            "text": (
                f"Spawned a native background agent on the local model. "
                f"agent id: {short_id}\n"
                f"It appears in `claude agents`. Supervise it cheaply with "
                f"watch_delegate({short_id!r}) (its plain-text plan + step narration, no code); "
                f"check_delegate_status({short_id!r}) for state + tokens; "
                f"get_delegate_result({short_id!r}) for the final answer.\n"
                f"To course-correct if it drifts: stop_delegate({short_id!r}), then "
                f"delegate_to_local again with a sharper task (the stopped run stays "
                f"readable via get_delegate_result). A running local agent will NOT read a "
                f"mid-run SendMessage; use SendMessage only to answer it when it is `blocked`."
            ),
        }],
        "isError": False,
    }


def check_status(args):
    handle = args.get("run_id") or args.get("agent_id")
    if not handle:
        return _error_result("`run_id` (the agent id) is required.")

    agent, err = _resolve_agent(handle)
    if err:
        return _error_result(err)
    if agent is None:
        return _error_result(
            f"No background agent with id {handle} in `claude agents --json`. "
            "It may have been removed, or the supervisor restarted. Run "
            "`claude agents --all --json` yourself to check."
        )

    state = agent.get("state") or agent.get("status") or "unknown"
    session_id = agent.get("sessionId") or ""

    text = (
        f"agent {agent.get('id')}: native state = {state}\n"
        f"  cwd: {agent.get('cwd')}\n"
        f"  session: {session_id}"
    )

    # Cheap always-on supervision: the task it was given, its last plain sentence,
    # and tokens burned so far -- so "watch them always" costs almost nothing.
    tr_watch = _find_transcript(session_id) if session_id else None
    if tr_watch:
        prompt = _strip_preamble(_first_user_text(tr_watch))
        if prompt:
            oneline = " ".join(prompt.split())
            text += f"\n  task: {oneline[:200]}" + ("…" if len(oneline) > 200 else "")
        last_said = ""
        for ln in _narration_digest(tr_watch, max_lines=0):
            if ln.startswith("["):
                last_said = ln
        if last_said:
            text += f"\n  last said: {last_said[:280]}"
        inp, out, turns = _token_usage(tr_watch)
        text += f"\n  cost so far: ~{out:,} output tokens over {turns} model turns"

    # If the agent is `blocked` (the native "needs input" state), classify WHY and
    # surface its last words so the parent (main model) can act without re-reading
    # the whole transcript. waitingFor is Claude Code's own signal; the transcript
    # tail refines it (gateguard / missing MCP / broken WebSearch / permission / ...).
    if state == "blocked":
        tr = _find_transcript(session_id) if session_id else None
        category, waiting_for, last_words = _classify_blocked(agent, tr)
        detail = f"  reason: {category}"
        if waiting_for:
            detail += f" (native waitingFor: {waiting_for!r})"
        text += "\n" + detail
        if last_words:
            text += f"\n\nIts last words:\n{last_words[:600]}"
        text += (
            "\n\nTo unblock: use the native SendMessage tool addressed to this agent "
            "(or `claude attach <id>`). Categories that usually need a settings fix "
            "instead of a message: mcp-tool-missing (check ~/.claude.json), "
            "websearch-broken (use the curl fallback), hook-gate (use the slim profile)."
        )

    return {"content": [{"type": "text", "text": text}], "isError": False}


def get_result(args):
    handle = args.get("run_id") or args.get("agent_id")
    if not handle:
        return _error_result("`run_id` (the agent id) is required.")

    agent, err = _resolve_agent(handle)
    if err:
        return _error_result(err)
    if agent is None:
        return _error_result(f"No background agent with id {handle} found in the roster.")

    state = agent.get("state") or agent.get("status") or "unknown"
    session_id = agent.get("sessionId") or ""

    # Still working? Don't hand back a partial answer.
    if state in ("working", "busy", "unknown"):
        return _error_result(
            f"agent {handle} is still {state} -- call check_delegate_status first. "
            f"get_delegate_result returns its final answer only once it has settled "
            f"(completed/idle/stopped)."
        )

    tr = _find_transcript(session_id) if session_id else None
    if not tr:
        return _error_result(
            f"Could not find a native transcript for session {session_id}. "
            f"Its state is {state}. The agent may have been cleaned up."
        )

    result_text = _last_assistant_text(tr)
    if result_text is None:
        return _error_result(
            f"agent {handle} has no assistant text in its transcript (state {state}). "
            "It may have errored before producing output -- check `claude logs "
            f"{agent.get('id')}`."
        )

    footer = (
        f"\n\n-- native agent id {agent.get('id')}, session {session_id}, "
        f"state {state}. This is the agent's own final answer from its transcript; "
        "review it before treating it as final (a local-model agent can look "
        "plausible while being subtly wrong)."
    )
    return {"content": [{"type": "text", "text": result_text + footer}], "isError": False}


def watch_delegate(args):
    """Token-cheap supervision: the agent's ORIGINAL task + its plain-text
    narration (plan, per-step intent, per-step outcome) with all tool output and
    code stripped, plus tokens burned. This is the 'watch it like a human runs
    the chat, read its messages not its code' view."""
    handle = args.get("run_id") or args.get("agent_id")
    if not handle:
        return _error_result("`run_id` (the agent id) is required.")

    agent, err = _resolve_agent(handle)
    if err:
        return _error_result(err)
    if agent is None:
        return _error_result(
            f"No background agent with id {handle} in `claude agents --json`."
        )

    state = agent.get("state") or agent.get("status") or "unknown"
    session_id = agent.get("sessionId") or ""
    tr = _find_transcript(session_id) if session_id else None
    if not tr:
        return _error_result(
            f"No transcript yet for {handle} (state {state}). It may have just "
            "started -- try again in a few seconds."
        )

    prompt = _strip_preamble(_first_user_text(tr)) or "(prompt not found)"
    max_lines = args.get("max_lines")
    try:
        max_lines = int(max_lines) if max_lines is not None else 48
    except (TypeError, ValueError):
        max_lines = 48
    digest = _narration_digest(tr, max_lines=max_lines)
    inp, out, turns = _token_usage(tr)

    body = (
        f"WATCHING agent {agent.get('id')} -- native state: {state}\n\n"
        f"--- ORIGINAL TASK -------------------------------\n"
        f"{prompt[:1400]}\n\n"
        f"--- PROGRESS (plain narration; tool output & code omitted) ---\n"
        + ("\n".join(digest) if digest else "(no assistant messages yet)")
        + f"\n\n--- COST --- ~{out:,} output tokens over {turns} model turns "
        f"(input+cache ~{inp:,})."
    )
    if state == "blocked":
        body += (
            f"\n\nIt is `blocked` on its own question (last words above). Answer it "
            f"with the native SendMessage tool addressed to this agent -- that is the "
            f"one case where a delegated agent reliably reads a message."
        )
    elif state == "working":
        body += (
            f"\n\nGoing the wrong way? stop_delegate({handle!r}) to halt it (settles in "
            f"~10-15s), then delegate_to_local again with a sharper task -- its stopped "
            f"transcript stays readable via get_delegate_result. Do NOT SendMessage a "
            f"correction to a running agent: it won't read it until the run ends. "
            f"On track? Just call watch_delegate again later."
        )
    return {"content": [{"type": "text", "text": body}], "isError": False}


def stop_delegate(args):
    """Halt a delegated agent by signalling its process (pid from the native
    roster). mode 'interrupt' = SIGINT (ask it to drop the current step),
    'terminate' = SIGTERM (end the run). The agent settles to `done` in ~10-15s
    and is then NOT reachable by SendMessage -- course-correct by calling
    delegate_to_local again with a sharper task (the stopped run's transcript
    stays readable via get_delegate_result).
    The native equivalent is the TaskStop tool with the agent's name."""
    handle = args.get("run_id") or args.get("agent_id")
    if not handle:
        return _error_result("`run_id` (the agent id) is required.")
    mode = (args.get("mode") or "interrupt").strip().lower()
    if mode not in ("interrupt", "terminate"):
        return _error_result("`mode` must be 'interrupt' or 'terminate'.")

    agent, err = _resolve_agent(handle)
    if err:
        return _error_result(err)
    if agent is None:
        return _error_result(
            f"No background agent with id {handle} in `claude agents --json`."
        )

    pid = agent.get("pid")
    state = agent.get("state") or agent.get("status") or "unknown"
    if state in ("completed", "failed", "stopped", "done", "idle"):
        return {"content": [{"type": "text", "text": (
            f"agent {handle} is already {state}; nothing to stop. To make progress on "
            f"a new direction, call delegate_to_local again with a sharper task "
            f"(this agent's transcript stays readable via get_delegate_result)."
        )}], "isError": False}
    if not pid:
        return _error_result(
            f"agent {handle} has no pid in the roster (state {state}). Use the "
            f"native TaskStop tool with its name instead."
        )

    sig = signal.SIGINT if mode == "interrupt" else signal.SIGTERM
    try:
        os.kill(int(pid), sig)
    except ProcessLookupError:
        return {"content": [{"type": "text", "text": (
            f"agent {handle} (pid {pid}) is already gone."
        )}], "isError": False}
    except (PermissionError, ValueError, OSError) as exc:
        return _error_result(
            f"Could not signal pid {pid}: {exc}. Use the native TaskStop tool "
            f"with the agent's name."
        )

    return {"content": [{"type": "text", "text": (
        f"Sent {sig.name} to agent {handle} (pid {pid}, was {state}).\n"
        f"Give it ~10-15s, then check_delegate_status to confirm it is `done`. "
        f"Then course-correct by calling delegate_to_local again with a sharper task -- "
        f"read what it already produced with get_delegate_result({handle!r}) and fold "
        f"anything useful into the new task text. (A stopped agent is not reachable by "
        f"SendMessage.) If SIGINT did not settle it, retry with mode='terminate'."
    )}], "isError": False}


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
    permission_mode = args.get("permission_mode")
    disallowed_tools = args.get("disallowed_tools")
    agent = args.get("agent")  # None -> default persona; "" -> no persona; name -> that persona
    announce_plan = args.get("announce_plan")
    batch_tag = uuid.uuid4().hex[:4]

    agent_ids = []
    for i, item in enumerate(items):
        task = f"{shared_instruction}\n\n--- item {i + 1}/{len(items)} ---\n\n{item}"
        name = _slug_from_task(f"fanout {batch_tag} item {i + 1} " + item.splitlines()[0])
        short_id, err = _spawn_native_agent(
            task, allowed_tools, cwd, name, permission_mode, disallowed_tools, agent,
            announce_plan,
        )
        if err:
            return _error_result(
                f"Failed to spawn item {i + 1}/{len(items)}: {err}\n"
                f"{len(agent_ids)} item(s) already spawned: {agent_ids}"
            )
        agent_ids.append(short_id)

    batch_id = uuid.uuid4().hex[:12]
    os.makedirs(BATCHES_DIR, exist_ok=True)
    with open(_batch_path(batch_id), "w") as f:
        json.dump({"batch_id": batch_id, "agent_ids": agent_ids,
                   "shared_instruction": shared_instruction, "created_at": time.time()}, f)

    warning = ""
    if len(items) > LOCAL_SERVER_MAX_CONCURRENCY:
        warning = (
            f"\n\nNote: {len(items)} agents spawned, but the local server's concurrency "
            f"ceiling is {LOCAL_SERVER_MAX_CONCURRENCY} (vLLM --max-num-seqs) and, more "
            "importantly, its shared KV-cache pool. The extras queue rather than running "
            "truly in parallel -- correctness is fine, just not full-parallel speed."
        )
    return {
        "content": [{
            "type": "text",
            "text": (f"Spawned {len(agent_ids)} parallel native agents. batch_id: {batch_id}\n"
                     f"agent ids: {agent_ids}\n"
                     f"Poll check_fanout_status({batch_id!r}), then get_fanout_result({batch_id!r}).{warning}"),
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

    agents = _agents_json(include_completed=True) or []
    by_id = {a.get("id"): a for a in agents}
    counts = {"working": 0, "blocked": 0, "completed": 0, "failed": 0, "stopped": 0, "unknown": 0}
    lines = []
    for aid in batch["agent_ids"]:
        a = by_id.get(aid)
        st = (a or {}).get("state") or (a or {}).get("status") or "unknown"
        counts[st] = counts.get(st, 0) + 1
        lines.append(f"  - {aid}: {st}")

    text = (
        f"batch_id {batch_id}: "
        f"{counts.get('working', 0)} working, {counts.get('blocked', 0)} blocked(needs input), "
        f"{counts.get('completed', 0)} completed, {counts.get('failed', 0)} failed, "
        f"{counts.get('stopped', 0)} stopped, {counts.get('unknown', 0)} unknown "
        f"(of {len(batch['agent_ids'])} total).\n"
        + "\n".join(lines)
        + "\nWhen none are working/blocked, call get_fanout_result."
    )
    return {"content": [{"type": "text", "text": text}], "isError": False}


def get_fanout_result(args):
    batch_id = args.get("batch_id")
    path = _batch_path(batch_id) if batch_id else None
    if not path or not os.path.isfile(path):
        return _error_result(f"Unknown batch_id: {batch_id}")
    with open(path) as f:
        batch = json.load(f)

    agents = _agents_json(include_completed=True) or []
    by_id = {a.get("id"): a for a in agents}

    parts = []
    incomplete = []
    for i, aid in enumerate(batch["agent_ids"]):
        a = by_id.get(aid)
        st = (a or {}).get("state") or (a or {}).get("status") or "unknown"
        sid = (a or {}).get("sessionId") or ""
        if st in ("working", "busy", "blocked", "unknown") or not sid:
            incomplete.append(f"{aid} ({st})")
            continue
        tr = _find_transcript(sid)
        txt = _last_assistant_text(tr) if tr else "(no transcript found)"
        parts.append(f"--- item {i + 1}/{len(batch['agent_ids'])} [{aid}] ---\n{txt}")

    if incomplete:
        return _error_result(
            f"These agents in batch {batch_id} are not settled yet: {incomplete}. "
            "Call check_fanout_status first; fetch results only once all are done."
        )
    return {"content": [{"type": "text", "text": "\n\n".join(parts)}], "isError": False}


# ---- verified-delegation loop (work -> check -> revise, all local) ----------
#
# Shape: a lazily-advanced state machine persisted to one JSON file per run.
#   phase "working"  -> a worker `claude --bg` agent is (re)doing the task
#   phase "checking" -> a checker `claude --bg` agent is independently verifying
#   phase "passed"   -> terminal; final_answer holds the signed-off result
#   phase "failed"   -> terminal; failure_report explains why (cap / stagnation /
#                        timeout / a crashed sub-agent), last candidate kept
#
# _advance_verified() is called on every check_verified_status / get_verified_result
# and moves the run AT MOST one transition per call by reading the sub-agents'
# NATIVE state (the same `claude agents --json` the rest of this file uses). No
# sleeping, no blocking -- the parent's polling cadence drives the loop.

_VERDICT_RE = re.compile(r"VERDICT:\s*(PASS|FAIL)", re.IGNORECASE)


def _verified_path(vid):
    return os.path.join(VERIFIED_DIR, f"{vid}.json")


def _load_verified(vid):
    path = _verified_path(vid) if vid else None
    if not path or not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def _save_verified(state):
    os.makedirs(VERIFIED_DIR, exist_ok=True)
    tmp = _verified_path(state["vid"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, _verified_path(state["vid"]))


def _result_hash(text):
    """Whitespace-normalised hash of a candidate answer, for stagnation detection
    (worker emits a byte-identical result two rounds running -> it is stuck)."""
    norm = " ".join((text or "").split())
    return hashlib.sha1(norm.encode("utf-8", "replace")).hexdigest()


def _parse_verdict(text):
    """Last `VERDICT: PASS|FAIL` token in the checker's final message. None if the
    checker never emitted one -- treated by the caller as a FAIL (can't confirm)."""
    hits = _VERDICT_RE.findall(text or "")
    return hits[-1].upper() if hits else None


def _agent_settled_state(agent_id):
    """(state, session_id) for a sub-agent id, via the native roster. state is one
    of working/blocked/completed/failed/stopped/done/idle/unknown/missing."""
    agent, _ = _resolve_agent(agent_id)
    if agent is None:
        return "missing", ""
    st = agent.get("state") or agent.get("status") or "unknown"
    return st, (agent.get("sessionId") or "")


_TERMINAL_OK = ("completed", "done", "idle")
_TERMINAL_BAD = ("failed", "stopped")


def _spawn_verify_worker(state):
    """Spawn the worker for the current iteration. On the first round the task is
    the raw spec; on later rounds the previous checker's reasons are prepended as
    a revision brief. Returns (worker_id, err)."""
    it = state["iteration"]
    spec = state["spec"]
    accept = state.get("acceptance")
    parts = [spec]
    if accept:
        parts.append("\n\n--- ACCEPTANCE CRITERIA (you must satisfy every one) ---\n" + accept)
    if it > 1 and state["history"]:
        last = state["history"][-1]
        parts.append(
            f"\n\n--- REVISION ROUND {it} ---\n"
            "A previous attempt was REJECTED by independent verification. "
            "Checker feedback:\n"
            f"{last.get('reasons') or '(no reasons recorded)'}\n\n"
            "Fix exactly these problems and redo the task. Work IN PLACE at the "
            "same paths. Do not start over from scratch unless the feedback says to."
        )
    task = "".join(parts)
    # Name is cosmetic (the run is tracked by vid in the state file), but it MUST
    # NOT contain the vid or any long hex token: _parse_bg_id scans the spawn
    # output for a hex id and would grab an echoed --name instead of the real
    # agent id. Keep it plain-alpha.
    return _spawn_native_agent(
        task, state["allowed_tools"], state["cwd"],
        f"verified-worker-r{it}",
        permission_mode=None, disallowed_tools=None, agent=None, announce_plan=None,
    )


def _spawn_verify_checker(state, candidate):
    """Spawn the adversarial checker for the current iteration. It gets the spec,
    the criteria, and the worker's self-reported result, and must verify against
    the ACTUAL working tree. Returns (checker_id, err)."""
    it = state["iteration"]
    spec = state["spec"]
    accept = state.get("acceptance")
    task = (
        "You are the INDEPENDENT VERIFIER for a task another agent just did in "
        f"this working directory ({state['cwd']}). Do NOT trust its self-report; "
        "check the real files and RUN things.\n\n"
        "--- ORIGINAL TASK ---\n" + spec + "\n\n"
        + (("--- ACCEPTANCE CRITERIA ---\n" + accept + "\n\n") if accept else
           "--- ACCEPTANCE CRITERIA ---\n(none supplied -- derive reasonable checks "
           "from the task itself and say which you used)\n\n")
        + "--- WORKER'S SELF-REPORTED RESULT ---\n" + (candidate or "(empty)") + "\n\n"
        "--- YOUR JOB ---\n"
        "1. Inspect the actual changes on disk (git diff / read the files).\n"
        "2. Build / run tests / run lint as applicable, and any check the criteria imply.\n"
        "3. Decide if the task is genuinely, completely done and not broken.\n"
        "You must FIX NOTHING. You have no Edit/Write tools on purpose.\n\n"
        "END your final message with, on its own line, exactly:\n"
        "  VERDICT: PASS   (if every criterion is met and nothing is broken)\n"
        "  VERDICT: FAIL   (otherwise)\n"
        "then 1-6 lines of concrete reasons: what you ran, what passed, what failed, "
        "and for a FAIL the smallest change that would fix it."
    )
    return _spawn_native_agent(
        task, VERIFY_CHECKER_TOOLS, state["cwd"],
        f"verified-checker-r{it}",  # plain-alpha: no vid / hex -- see _spawn_verify_worker
        permission_mode=None, disallowed_tools=None,
        agent=(CHECKER_PERSONA if _persona_exists(CHECKER_PERSONA) else ""),
        announce_plan=None,
    )


def _persona_exists(name):
    return bool(name) and os.path.isfile(os.path.expanduser(f"~/.claude/agents/{name}.md"))


def _fail_verified(state, report):
    state["phase"] = "failed"
    state["failure_report"] = report
    _save_verified(state)
    return f"FAILED: {report}"


def _advance_verified(state):
    """Move the run forward at most one transition. Returns a human-readable
    one-liner describing the (possibly unchanged) phase. Saves on any change."""
    phase = state["phase"]
    if phase in ("passed", "failed"):
        return f"{phase.upper()} (terminal)"

    # Global wall-clock guard -- applies in any non-terminal phase.
    elapsed = time.time() - state["created_at"]
    if elapsed > state["timeout"]:
        return _fail_verified(
            state,
            f"timeout after {int(elapsed)}s (limit {state['timeout']}s), "
            f"stuck in phase '{phase}' on iteration {state['iteration']}",
        )

    it = state["iteration"]

    if phase == "working":
        wid = state["current_worker_id"]
        st, sid = _agent_settled_state(wid)
        if st in ("working", "blocked", "unknown"):
            return f"iteration {it}/{state['max_iters']}: worker {wid} is {st}"
        if st in _TERMINAL_BAD or st == "missing":
            return _fail_verified(state, f"worker {wid} ended in state '{st}' on iteration {it}")
        # worker settled OK -> grab its candidate answer, launch the checker
        tr = _find_transcript(sid) if sid else None
        candidate = (_last_assistant_text(tr) if tr else None) or ""
        cand_hash = _result_hash(candidate)
        prev_hash = state["history"][-1]["cand_hash"] if state["history"] else None
        state["history"].append({
            "iter": it, "worker_id": wid, "cand_hash": cand_hash,
            "candidate": candidate, "checker_id": None, "verdict": None, "reasons": None,
        })
        # Stagnation: identical candidate two rounds running -> the loop cannot
        # make progress, stop now instead of burning the remaining iterations.
        if prev_hash is not None and cand_hash == prev_hash:
            last_reasons = state["history"][-2].get("reasons") if len(state["history"]) >= 2 else None
            return _fail_verified(
                state,
                "stagnation: worker produced a byte-identical result two rounds "
                f"running without passing. Last checker feedback: {last_reasons or '(none)'}",
            )
        cid, err = _spawn_verify_checker(state, candidate)
        if err:
            return _fail_verified(state, f"could not spawn checker for iteration {it}: {err}")
        state["history"][-1]["checker_id"] = cid
        state["current_checker_id"] = cid
        state["phase"] = "checking"
        _save_verified(state)
        return f"iteration {it}/{state['max_iters']}: worker done, checker {cid} launched"

    if phase == "checking":
        cid = state["current_checker_id"]
        st, sid = _agent_settled_state(cid)
        if st in ("working", "blocked", "unknown"):
            return f"iteration {it}/{state['max_iters']}: checker {cid} is {st}"
        if st in _TERMINAL_BAD or st == "missing":
            return _fail_verified(state, f"checker {cid} ended in state '{st}' on iteration {it}")
        tr = _find_transcript(sid) if sid else None
        checker_text = (_last_assistant_text(tr) if tr else None) or ""
        verdict = _parse_verdict(checker_text)
        rec = state["history"][-1]
        rec["verdict"] = verdict or "FAIL(no-verdict)"
        rec["reasons"] = checker_text[-2000:]
        if verdict == "PASS":
            state["phase"] = "passed"
            state["final_answer"] = rec["candidate"]
            _save_verified(state)
            return f"PASSED on iteration {it}/{state['max_iters']}"
        # FAIL (or no parseable verdict) -> revise if we have iterations left
        if it >= state["max_iters"]:
            return _fail_verified(
                state,
                f"did not pass verification in {state['max_iters']} iteration(s). "
                f"Last checker feedback: {(checker_text or '(none)')[-1200:]}",
            )
        state["iteration"] = it + 1
        wid, err = _spawn_verify_worker(state)
        if err:
            return _fail_verified(state, f"could not spawn worker for iteration {it + 1}: {err}")
        state["current_worker_id"] = wid
        state["current_checker_id"] = None
        state["phase"] = "working"
        _save_verified(state)
        return f"iteration {it} FAILED, revision round {it + 1} launched (worker {wid})"

    return f"unknown phase '{phase}'"


def _verify_trail(state):
    """Compact per-iteration audit line for the status/result output."""
    lines = []
    for h in state["history"]:
        lines.append(
            f"  r{h['iter']}: worker {h['worker_id']} -> checker "
            f"{h.get('checker_id') or '-'} -> {h.get('verdict') or 'pending'}"
        )
    return "\n".join(lines) if lines else "  (no iterations yet)"


def delegate_verified(args):
    task = args.get("task")
    if not task or not isinstance(task, str):
        return _error_result("`task` is required and must be a non-empty string.")

    acceptance = args.get("acceptance_criteria")
    if acceptance is not None and not isinstance(acceptance, str):
        return _error_result("`acceptance_criteria`, if given, must be a string.")
    cwd = args.get("cwd") or os.getcwd()
    allowed_tools = args.get("allowed_tools") or DEFAULT_VERIFY_WORKER_TOOLS
    try:
        max_iters = int(args.get("max_iterations") or DEFAULT_MAX_VERIFY_ITERS)
    except (TypeError, ValueError):
        return _error_result("`max_iterations` must be an integer.")
    max_iters = max(1, min(max_iters, 10))
    try:
        timeout = int(args.get("timeout_seconds") or DEFAULT_VERIFY_TIMEOUT)
    except (TypeError, ValueError):
        return _error_result("`timeout_seconds` must be an integer.")

    vid = uuid.uuid4().hex[:12]
    state = {
        "vid": vid,
        "created_at": time.time(),
        "phase": "working",
        "spec": task,
        "acceptance": acceptance,
        "cwd": cwd,
        "allowed_tools": allowed_tools,
        "max_iters": max_iters,
        "timeout": timeout,
        "iteration": 1,
        "current_worker_id": None,
        "current_checker_id": None,
        "history": [],
        "final_answer": None,
        "failure_report": None,
    }
    wid, err = _spawn_verify_worker(state)
    if err:
        return _error_result(f"Failed to spawn the first worker: {err}")
    state["current_worker_id"] = wid
    _save_verified(state)

    return {
        "content": [{
            "type": "text",
            "text": (
                f"Started a VERIFIED local delegation. vid: {vid}\n"
                f"iteration 1/{max_iters}, worker agent: {wid} "
                f"(also visible in `claude agents`).\n\n"
                "This runs a closed work -> independent-check -> revise loop "
                "ENTIRELY on the local model. Poll check_verified_status("
                f"{vid!r}); it advances the loop one step per call. Call "
                f"get_verified_result({vid!r}) only once it reports PASSED or "
                "FAILED -- you then get a result a local checker has signed off "
                "on, or a failure report with the checker's reasons. "
                f"Guards: {max_iters} iterations max, stagnation detection, "
                f"{timeout}s wall-clock."
            ),
        }],
        "isError": False,
    }


def check_verified_status(args):
    vid = args.get("vid") or args.get("run_id")
    if not vid:
        return _error_result("`vid` (from delegate_verified) is required.")
    state = _load_verified(vid)
    if state is None:
        return _error_result(f"Unknown vid: {vid}")

    line = _advance_verified(state)
    phase = state["phase"]
    elapsed = int(time.time() - state["created_at"])
    text = (
        f"verified run {vid}: phase = {phase} ({line})\n"
        f"  elapsed: {elapsed}s / {state['timeout']}s   iterations: "
        f"{state['iteration']}/{state['max_iters']}\n"
        f"  trail:\n{_verify_trail(state)}"
    )
    if phase == "passed":
        text += f"\n\nDONE -- call get_verified_result({vid!r}) for the signed-off answer."
    elif phase == "failed":
        text += (
            f"\n\nSTOPPED -- call get_verified_result({vid!r}) for the failure report "
            "plus the last candidate answer (not discarded)."
        )
    else:
        text += "\n\nStill running. Poll check_verified_status again to advance it."
    return {"content": [{"type": "text", "text": text}], "isError": False}


def get_verified_result(args):
    vid = args.get("vid") or args.get("run_id")
    if not vid:
        return _error_result("`vid` (from delegate_verified) is required.")
    state = _load_verified(vid)
    if state is None:
        return _error_result(f"Unknown vid: {vid}")

    _advance_verified(state)
    phase = state["phase"]
    if phase not in ("passed", "failed"):
        return _error_result(
            f"verified run {vid} is still {phase} (iteration "
            f"{state['iteration']}/{state['max_iters']}). Call check_verified_status "
            "until it reports PASSED or FAILED."
        )

    trail = _verify_trail(state)
    if phase == "passed":
        body = (
            f"VERIFIED PASS -- vid {vid}, signed off on iteration {state['iteration']}"
            f"/{state['max_iters']} by an independent local checker.\n\n"
            f"--- VERIFICATION TRAIL ---\n{trail}\n\n"
            f"--- SIGNED-OFF RESULT ---\n{state['final_answer'] or '(worker produced no text)'}"
        )
        return {"content": [{"type": "text", "text": body}], "isError": False}

    last = state["history"][-1] if state["history"] else {}
    body = (
        f"VERIFIED FAIL -- vid {vid}. {state['failure_report']}\n\n"
        f"--- VERIFICATION TRAIL ---\n{trail}\n\n"
        f"--- LAST CHECKER FEEDBACK ---\n{(last.get('reasons') or '(none)')}\n\n"
        f"--- LAST CANDIDATE (unverified, kept so it is not lost) ---\n"
        f"{last.get('candidate') or '(none)'}\n\n"
        "The working tree still holds the last worker's changes. Inspect them, or "
        "re-run delegate_verified with tighter acceptance_criteria."
    )
    return {"content": [{"type": "text", "text": body}], "isError": True}


# ---- tool schema (parent side only) ------------------------------------------

PARENT_TOOLS = [
    {
        "name": "delegate_to_local",
        "description": (
            "Delegate a self-contained task to a NATIVE Claude Code background agent "
            "(`claude --bg`) running on the local model (vLLM/LiteLLM via --settings). "
            "Unlike an in-session subagent, this agent is a real top-level Claude Code "
            "session: it shows up in `claude agents`, can be inspected with "
            "`claude logs <id>` / `claude attach <id>`, and the parent can send it "
            "follow-up requests through the native SendMessage tool (it appears in "
            "ListAgents while running). Returns immediately with the agent's native id "
            "-- this does NOT block, so call it several times back-to-back to run "
            "delegations in parallel (bounded by the local server's concurrency). "
            "Poll with check_delegate_status, then read the answer with "
            "get_delegate_result. Use for mechanical/high-volume work where local-model "
            "latency is worth saving paid tokens; keep architecture decisions and "
            "security-sensitive work in the main session.\n"
            "PREFER MANY SMALL TASKS over one long run: a delegation you can describe "
            "in one outcome ('rename X to Y across src/', 'add tests for module Z') is "
            "easy to watch and cheap to redo if it drifts; a sprawling one is neither. "
            "Split big work and delegate the pieces (or use fan_out_to_local). "
            "Supervise with watch_delegate (plan + step narration, no code). If it "
            "drifts: stop_delegate, then delegate_to_local again with a sharper task -- "
            "a RUNNING local agent does NOT read a mid-run SendMessage (it finishes its "
            "run first), so reserve SendMessage for answering an agent that is `blocked` "
            "on its own question. The stopped run stays readable via get_delegate_result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The self-contained task. The agent starts with no memory of this conversation."},
                "allowed_tools": {"type": "string", "description": f"Comma-separated tools the agent may use without prompting. Defaults to read-only ('{DEFAULT_ALLOWED_TOOLS}'). Widen only when it must write files or run commands -- it runs unsupervised."},
                "cwd": {"type": "string", "description": "Working directory for the agent. Defaults to this server's cwd."},
                "name": {"type": "string", "description": "Optional display name for the agent (shown in `claude agents`). Defaults to a short slug of the task."},
                "permission_mode": {"type": "string", "description": f"Native --permission-mode for the background agent. Defaults to '{DEFAULT_PERMISSION_MODE}' so the unattended agent runs its full granted toolset (Bash included) without approval gates -- a delegated agent that prompts on Bash would park forever with no one to approve. Set to 'acceptEdits' (Bash gated) or 'default' (everything prompts) to narrow a specific delegation, or 'auto' for the classifier-gated middle ground."},
                "disallowed_tools": {"type": "string", "description": "Comma-separated tools to strip from the agent (e.g. 'Bash'). Useful when a user-level hook/gate blocks a tool the agent would reach for on its own -- disallowing that tool forces the agent onto a path it can complete. Default: none."},
                "agent": {"type": "string", "description": f"Subagent-definition persona to run the delegation as (its system prompt). Defaults to 'local-worker' (~/.claude/agents/local-worker.md: verifies by running a check, returns evidence, states assumptions). Pass another installed agent name to override, or empty string to run with no persona."},
                "announce_plan": {"type": "boolean", "description": f"Prepend the supervision preamble that makes the agent post a numbered plan first and narrate each step in one plain sentence (what watch_delegate shows the parent). Default {DEFAULT_ANNOUNCE_PLAN}. Set false only for a trivial one-shot where narration is noise."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "check_delegate_status",
        "description": (
            "Read the NATIVE state of a delegated background agent by its id (from "
            "delegate_to_local): working / blocked / completed / failed / stopped. "
            "Also surfaces, cheaply, the task it was given, its last plain-text "
            "sentence, and output tokens burned so far. 'blocked' is the agent's "
            "native 'I need input' signal -- its last words (its question) are shown "
            "so you can answer with the native SendMessage tool. For the full "
            "step-by-step narration use watch_delegate. Does not touch the model."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The agent id returned by delegate_to_local (a.k.a. the `claude agents` short id)."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "get_delegate_result",
        "description": (
            "Read the delegated agent's final answer from its NATIVE transcript. "
            "Errors if the agent is still working/blocked -- call check_delegate_status "
            "first. IMPORTANT: review this before treating it as final; a local-model "
            "agent can produce plausible-looking but subtly wrong output that only a "
            "read-through catches."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The agent id returned by delegate_to_local."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "watch_delegate",
        "description": (
            "Token-cheap supervision of a delegated agent: its ORIGINAL task plus "
            "its plain-text narration (numbered plan, one sentence of intent before "
            "each step, one sentence of outcome after), with ALL tool output, file "
            "contents and code stripped out -- plus output tokens burned. This is the "
            "'watch it like a human running the chat: read its messages, not its "
            "code' view. Call it repeatedly to follow a run. If the narration shows "
            "it drifting: stop_delegate, then re-delegate a smaller, sharper task "
            "(delegate_to_local). Do NOT try to SendMessage a correction to a running "
            "agent -- a local `claude --bg` agent won't read it until its run ends, by "
            "which point it is `done` and unreachable; SendMessage is only for "
            "answering an agent that is `blocked` on its own question."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The agent id returned by delegate_to_local."},
                "max_lines": {"type": "integer", "description": "Max narration lines to return (tail). Default 48; <=0 for the whole run."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "stop_delegate",
        "description": (
            "Halt a delegated agent that is going the wrong way. Signals its process "
            "(pid from the native roster): mode 'interrupt' (default, SIGINT) asks it "
            "to drop the current step; mode 'terminate' (SIGTERM) ends the run. The "
            "agent settles to `done` in ~10-15s (before its next step) and is then "
            "NOT reachable by SendMessage -- recover by calling delegate_to_local "
            "again with a smaller, sharper task; the stopped run's transcript stays "
            "readable via get_delegate_result, so fold anything useful it already did "
            "into the new task. This is the reliable way to course-correct: a running "
            "local agent will not read a mid-run SendMessage. "
            "Native equivalent: the TaskStop tool with the agent's name."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "The agent id returned by delegate_to_local."},
                "mode": {"type": "string", "enum": ["interrupt", "terminate"], "description": "'interrupt' (SIGINT, default) or 'terminate' (SIGTERM)."},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "fan_out_to_local",
        "description": (
            "Map-reduce over the local model using NATIVE background agents: spawn one "
            "`claude --bg` agent per item, all sharing `shared_instruction`. Use this "
            "instead of stuffing everything into one giant local context (each item fits "
            "its own window; chunking also tends to beat one huge context on accuracy). "
            "Returns immediately with a batch_id; poll check_fanout_status, then "
            "get_fanout_result once all settle. The real ceiling is the local server's "
            "concurrency/KV-cache pool, not the number of items."
        ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "items": {"type": "array", "items": {"type": "string"}, "description": "Independent pieces of material, one per parallel agent."},
                    "shared_instruction": {"type": "string", "description": "The instruction applied to every item."},
                    "allowed_tools": {"type": "string", "description": f"Same as delegate_to_local, applied to every item. Defaults to read-only ('{DEFAULT_ALLOWED_TOOLS}')."},
                    "cwd": {"type": "string", "description": "Working directory for every agent."},
                    "permission_mode": {"type": "string", "description": f"Same as delegate_to_local's permission_mode, applied to every agent. Defaults to '{DEFAULT_PERMISSION_MODE}'."},
                    "disallowed_tools": {"type": "string", "description": "Same as delegate_to_local's disallowed_tools, applied to every agent."},
                    "agent": {"type": "string", "description": "Same as delegate_to_local's agent persona, applied to every agent. Defaults to 'local-worker'."},
                    "announce_plan": {"type": "boolean", "description": f"Same as delegate_to_local's announce_plan, applied to every agent. Default {DEFAULT_ANNOUNCE_PLAN}."},
                },
                "required": ["items", "shared_instruction"],
            },
    },
    {
        "name": "check_fanout_status",
        "description": "Aggregate the native states (working/blocked/completed/failed/stopped) of a fan_out_to_local batch.",
        "inputSchema": {"type": "object", "properties": {"batch_id": {"type": "string"}}, "required": ["batch_id"]},
    },
    {
        "name": "get_fanout_result",
        "description": "Read every agent's final answer from its native transcript for a fan_out_to_local batch. Errors until all agents are settled.",
        "inputSchema": {"type": "object", "properties": {"batch_id": {"type": "string"}}, "required": ["batch_id"]},
    },
    {
        "name": "delegate_verified",
        "description": (
            "Delegate a task to the local model AND have it independently verified "
            "by a second local agent before you ever see the answer. Runs a closed "
            "work -> check -> revise loop ENTIRELY on the local model: a worker "
            "`claude --bg` agent does the task, then an adversarial checker agent "
            "(no Edit/Write tools) re-verifies against the real working tree -- "
            "reads the diff, builds, runs tests/lint -- and emits VERDICT: "
            "PASS|FAIL. On FAIL the checker's reasons are fed back into a fresh "
            "worker round. You get back ONLY a result a checker signed off on, or "
            "a failure report with the reasons -- so the parent session never has "
            "to review raw local-model output or remember to. Use this instead of "
            "delegate_to_local when the task has a checkable outcome (code that "
            "must build / pass tests / meet stated criteria). Returns a `vid` "
            "immediately; poll check_verified_status(vid) to advance the loop "
            "(one step per call, nothing blocks), then get_verified_result(vid) "
            "once it says PASSED or FAILED. Guards against runaway loops: "
            f"{DEFAULT_MAX_VERIFY_ITERS} iterations max (override with "
            "max_iterations), stagnation detection (identical result twice -> "
            f"stop), and a {DEFAULT_VERIFY_TIMEOUT}s wall-clock ceiling."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The self-contained task for the worker. Include exact paths and what 'done' means. The worker starts with no memory of this conversation."},
                "acceptance_criteria": {"type": "string", "description": "Explicit pass/fail conditions the checker must confirm (e.g. 'pytest -q is green', 'ruff check passes', 'CLI prints X for input Y'). STRONGLY recommended -- without it the checker only derives its own best-guess checks and the gate is weak."},
                "allowed_tools": {"type": "string", "description": f"Comma-separated tools for the WORKER. Defaults to '{DEFAULT_VERIFY_WORKER_TOOLS}' (it must be able to change code). The checker's tools are fixed at '{VERIFY_CHECKER_TOOLS}' -- no Edit/Write, on purpose."},
                "cwd": {"type": "string", "description": "Working directory for both worker and checker. Defaults to this server's cwd."},
                "max_iterations": {"type": "integer", "description": f"Max work->check rounds before giving up. Default {DEFAULT_MAX_VERIFY_ITERS}, clamped to 1..10."},
                "timeout_seconds": {"type": "integer", "description": f"Wall-clock ceiling for the whole loop. Default {DEFAULT_VERIFY_TIMEOUT}. Past this the run is force-failed."},
            },
            "required": ["task"],
        },
    },
    {
        "name": "check_verified_status",
        "description": (
            "Advance and report a delegate_verified run. Each call moves the "
            "work->check->revise state machine forward at most one step by "
            "reading the sub-agents' native state -- so poll this the way you'd "
            "poll check_delegate_status. Shows the current phase (working / "
            "checking / passed / failed), elapsed vs timeout, iteration count, "
            "and a per-round trail (worker id -> checker id -> verdict). When it "
            "reports PASSED or FAILED, call get_verified_result."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vid": {"type": "string", "description": "The vid returned by delegate_verified."},
            },
            "required": ["vid"],
        },
    },
    {
        "name": "get_verified_result",
        "description": (
            "Fetch the outcome of a delegate_verified run. Errors (with the "
            "current phase) until the loop has settled. On PASS: the signed-off "
            "worker answer plus the verification trail. On FAIL: the failure "
            "reason (iteration cap / stagnation / timeout / crashed sub-agent), "
            "the last checker's feedback, and the last candidate answer (kept, "
            "not discarded -- the working tree still has its changes)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "vid": {"type": "string", "description": "The vid returned by delegate_verified."},
            },
            "required": ["vid"],
        },
    },
]


def _error_result(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


TOOL_HANDLERS = {
    "delegate_to_local": start_delegate,
    "check_delegate_status": check_status,
    "get_delegate_result": get_result,
    "watch_delegate": watch_delegate,
    "stop_delegate": stop_delegate,
    "fan_out_to_local": fan_out_to_local,
    "check_fanout_status": check_fanout_status,
    "get_fanout_result": get_fanout_result,
    "delegate_verified": delegate_verified,
    "check_verified_status": check_verified_status,
    "get_verified_result": get_verified_result,
}
TOOLS = PARENT_TOOLS


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
        return None

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