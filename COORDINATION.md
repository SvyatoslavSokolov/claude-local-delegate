# Shared Claude Code / Codex supervision

These instructions are for the supervising session. A delegated local-worker or
local-checker executes its assigned task; it must not follow supervisor delegation
rules or recursively delegate.

Keep only task framing, architecture, acceptance criteria and concise final review
on the main model. Delegate all substantive research, implementation, edits and
checks to the local backend first, and do not duplicate that work while it runs.
Prefer one focused worker unless tasks are truly independent, and request full
results only when the compact answer or diff is insufficient. Read TASK_DESIGN.md before briefing
nontrivial work. Give exact paths, a small outcome and executable checks. Do not
trust a worker's claimed model identity: `check_delegate_status` prints the
backend recorded at spawn time, and `local_backend_info` shows the configured
route. A run with no recorded backend was not spawned by this MCP.

State a task's acceptance criteria when you want it verified. `delegate_verified`
skips the checker round entirely when there are no criteria and nothing changed on
disk (or the run was read-only) -- there would be nothing objective to check --
and returns an explicit UNVERIFIED PASS. Pass `always_verify: true` to force it.

## Starting work

1. Call project_sync with the absolute project root. Inspect existing tasks and
   notes before doing work, including work you intend to do yourself.
2. Call task_claim with a stable descriptive task_key, summary, mode and paths.
   Use literal relative file/directory paths; a directory reserves its subtree.
   Default scope `.` reserves the entire project. Shell/build/tests need write
   mode because they may change files. Include files whose stable content the task
   depends on. A claim is always active: paths and declared depends_on are
   recorded and returned by project_sync (the raw reservations are visible so you
   can coordinate), but they never block, so a stale or "blocked" record cannot
   hold follow-on work.
3. Start a task once it is owned by this session (the session_id from
   project_sync). An existing task from another owner is not yours. Reuse the
   same task_key on retries; do not invent a new key to bypass duplicate
   detection. Similar wording is not semantic duplicate detection: read the
   summaries and agree on boundaries.
4. Nothing waits on blockers or dependencies. If project_sync shows another
   session's reservation touches the same paths, or a dependency is not done,
   that record is visible only -- start your follow-on work anyway and reconcile
   in review. There is no task_update(status="active") gate to retry.
5. Pass task_id to delegate_to_local, fan_out_to_local or delegate_verified.
   Prefer separate task reservations for separate writing workers. A write
   fan-out under one reservation does NOT isolate siblings from each other.
   Read-only fan-outs are appropriate.
6. For an ordinary run, call `get_delegate_result(wait_seconds=900)` and let that
   single MCP call wait and return the compact final answer. If it times out, call
   it again; use `check_delegate_status`/`watch_delegate` only when you need to
   inspect progress or diagnose drift. This avoids spending one main-model turn
   per routine poll. Verified loops still use `check_verified_status` because
   polling advances their state machine. Review the final result, diff and
   relevant checks. Then task_update(status="done", note=concise evidence).

## Checkpoints, conflicts and handoffs

Results come back compacted: `get_delegate_result` returns the tail of the
agent's answer plus a sha256 of the full text, and `get_fanout_result` compacts
each item. Pass `full: true` when you actually need the whole text -- the
transcript on disk is never truncated. In Codex, `output_token_limit` per tool in
~/.codex/config.toml caps this a second time on the client side.

Call project_sync before each new subtask and before changing shared interfaces,
applying results, commits or integration. Keep the returned next_event cursor and
pass after_event to avoid rereading notes. Post project_note for interface changes,
questions, handoff requests and blockers. Include task_id when relevant.

Messages are a shared mailbox read on sync: they do not interrupt or wake another
Codex/Claude session. A pause request is cooperative. project_sync shows each
session's reservation and its paths but does not compute or enforce overlap:
a "conflicting" or "blocked" record does not stop your follow-on work, so proceed
and reconcile in review rather than waiting.
task_update(status="paused") keeps the task and its paths; it does not suspend child processes.
Use status "waiting" only if you deliberately want to park a task; nothing forces it.
Never stop another supervisor's worker without agreeing with that supervisor.

Never automatically take over stale reservations: a disconnected supervisor can
have live workers. Session IDs are printed by project_sync/initialize. To recover
a disconnected supervisor, restart its MCP with CLAUDE_LOCAL_DELEGATE_SESSION_ID
set to that exact ID only after confirming the old MCP process is gone; never
share an ID between live supervisors. Review native agents and unfinished verified
loops, then finish/cancel the old task. Ordinary sessions use unique automatic IDs.

## Talking to local workers from either client

The executor is `claude --settings ~/.claude/vllm.delegate.settings.json --bg`,
using the local model even when its parent is Codex. The same worker/checker
personas in ~/.claude/agents and the same settings file are used by both clients.
They are not native Codex subagents. No second model profile or duplicated secrets
are needed. ~/.codex/local-delegate-agents is a convenience symlink to these
personas, not Codex's native agents configuration.

Codex has no native Claude SendMessage tool. Use stop_delegate, poll until settled,
then continue_delegate(run_id, message, allowed_tools, task_id). This forks the
native conversation and retains transcript context in a new background session.
The old transcript remains readable. This is an explicit restart/fork, not live
message injection. In Claude, native SendMessage remains usable for blocked agents.

## Limits

Reservations are checked before MCP spawning and are included in worker prompts;
they are not OS filesystem locks. Direct edits, shell commands and tools outside
this MCP can bypass them. A read-only delegation (allowlist of Read/Grep/Glob and
friends) now runs in `dontAsk`, where the allowlist is actually enforced and
anything unlisted is denied rather than prompted; a delegation that can write
still runs in `bypassPermissions`, which IGNORES `allowedTools` entirely, so for
writers the allowlist remains advice, not a sandbox. For strong isolation use
separate worktrees and a single integration owner; this version does not merge
worktrees or suspend a whole client.

The pool ceiling counts only background sessions this MCP recorded as local
delegates (~/.claude-local-delegate/runs.json), plus spawns still in flight
(~/.claude-local-delegate/inflight.json). A paid Anthropic background session --
a supervisor, for instance -- appears in the same native roster but no longer
consumes a slot in the local vLLM pool it never touches. The ceiling rejects
excess spawns for retry; it is not a persistent automatic scheduler.

Mutating calls are serialized across MCP processes; read-only calls
(project_sync, check_delegate_status, watch_delegate, get_delegate_result,
check_fanout_status, get_fanout_result, local_backend_info) are not, and a spawn
releases the shared lock while `claude --bg` starts, so one supervisor's spawn no
longer freezes the other's polling for up to two minutes. Restart both clients
after updating the server: older already-running server processes do not
participate in these protections.

Shared state lives in ~/.claude-local-delegate/coordination.sqlite3. Use one local
state directory for both clients (not separate copies and not a network filesystem).
Both processes must run as the same OS user. For a distinct environment, set
CLAUDE_LOCAL_DELEGATE_STATE_DIR identically in both MCP registrations.

## Codex registration

Run `python3 contrib/install_codex.py --apply`. It backs up changed global files,
adds one stdio server in ~/.codex/config.toml, appends links to these instructions
to ~/.codex/AGENTS.md and ~/.claude/CLAUDE.md, and creates the persona symlink.
The existing Claude MCP registration and vLLM settings remain the shared sources.
Without --apply it only prints the planned config and paths.

Official Codex MCP configuration reference:
https://learn.chatgpt.com/docs/extend/mcp?surface=cli
