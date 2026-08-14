# claude-local-delegate

An MCP server for Claude Code with tools to hand a task to a locally-hosted
model (e.g. served by vLLM) while keeping your main session on your regular
subscription/model. Parent-side: `delegate_to_local`, `check_delegate_status`,
`get_delegate_result`, `reply_to_delegate`. Given to the delegated run itself:
`ask_parent`, `check_message_status`.

## Why this instead of a prompt-wrapper MCP tool

`delegate_to_local` doesn't summarize context into a single completion
request. It spawns a real headless `claude -p` subprocess with `--settings`
pointed at your local backend. The delegated task runs through the actual
Claude Code agent loop — its own tool calls (Read/Edit/Bash/...), its own
context management, its own `CLAUDE.md` — just backed by a different model.
From the repo's point of view it *is* Claude Code, not an approximation of it.

This also keeps your subscription untouched: the local delegation is a
separate process using its own `--settings` file (pointed at a local
gateway), not a shared LLM gateway sitting in front of your main session —
which would otherwise disable subscription billing for *all* traffic through
it, including your main model.

## Async by design

`delegate_to_local` spawns the subprocess in the background (detached, its
own session) and returns a `run_id` immediately — it does not block the
calling session for however long the local model takes. Poll
`check_delegate_status(run_id)` for progress (with a log tail), then
`get_delegate_result(run_id)` once it's done.

Because the call returns immediately, firing several `delegate_to_local`
calls back-to-back runs them **genuinely in parallel** — the only limit is
the local server's own concurrency (e.g. vLLM's `--max-num-seqs`), not
anything in this tool. Each run gets its own working directory copy of
nothing shared except the filesystem, so parallel runs touching the same
files can still race each other — scope `allowed_tools`/`cwd` accordingly.

## Bi-directional: the delegated run can ask back

Each `delegate_to_local` run is launched with its own `--mcp-config` that
points back at this same `server.py`, spawned a second time as a **separate
process** with `CLAUDE_LOCAL_DELEGATE_ROLE=child` in its environment. In that
mode the script exposes only `ask_parent`/`check_message_status` — not
`delegate_to_local` itself, so a delegated run can't spawn further
delegations unboundedly.

When the local model calls `ask_parent`, it writes a question to a JSON file
under the run's `messages/` directory and blocks (server-side, inside
`check_message_status`, up to ~50s per call) waiting for an answer. The
parent session sees pending questions surfaced in `check_delegate_status`'s
output and answers with `reply_to_delegate`; the waiting child picks up the
answer on its next poll and continues. Parent and child are always different
OS processes — coordination is entirely through files in the run directory,
not shared memory.

This is genuinely load-bearing, not decorative: without it, a delegated run
that hits something it can't decide either guesses (silently, possibly
wrong) or fails outright. With it, it can stop and ask instead.

## Two different token budgets, two different defaults

The local model's tokens are free (self-hosted); the parent session's tokens
are the paid ones. The defaults reflect that asymmetry rather than treating
"fewer tokens" as universally good:

- **Local side, `bare` (default `false`):** loading full context (skills,
  plugins, hooks, CLAUDE.md) costs the local model tens of thousands of extra
  input tokens per call in testing — irrelevant, since those tokens are
  free, and full context can only help accuracy on tasks that touch anything
  project-specific. Set `bare: true` only when you're confident a task is
  generic enough not to need any of that (e.g. "summarize this text") and
  you want the latency win; CLAUDE.md is still re-added even in bare mode,
  since project conventions are worth keeping regardless.
- **Parent side, `check_delegate_status`:** this *is* paid-token territory,
  so it returns a compact progress summary (parsed from the run's
  stream-json log: assistant text, tool calls, tool results) instead of a
  raw log tail. A raw tail is mostly `system/init` noise — the full
  skill/tool catalog dump, easily thousands of tokens — for zero signal
  about what the delegated run is actually doing.

## Requirements

- `claude` CLI on `PATH` (Claude Code)
- A `--settings` file pointing Claude Code at your local model. Default path:
  `~/.claude/vllm.settings.json` (override with
  `CLAUDE_LOCAL_DELEGATE_SETTINGS`). See the
  [vllm](https://github.com/SvyatoslavSokolov/vllm) stack for an example of
  what that file needs (`ANTHROPIC_BASE_URL` pointed at a local
  Anthropic-compatible gateway, model env vars, etc).
- Python 3, stdlib only — no `pip install` required.

## Install

```bash
git clone https://github.com/SvyatoslavSokolov/claude-local-delegate.git
```

Register it as a user-scoped MCP server in `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "claude-local-delegate": {
      "command": "python3",
      "args": ["/absolute/path/to/claude-local-delegate/server.py"]
    }
  }
}
```

Restart Claude Code. The tools are now available in every session.

## Usage

Claude decides when to delegate based on the tool description (mechanical /
high-volume work: drafts, boilerplate, straightforward refactors,
summarization). You can also nudge it explicitly, or add a rule to your
`CLAUDE.md`:

```
For mechanical, high-volume, or low-risk work (boilerplate, drafts,
summaries, simple refactors), use delegate_to_local instead of doing it
yourself. Poll with check_delegate_status and read the answer with
get_delegate_result. You can start several delegations back-to-back to run
them in parallel. Keep architecture decisions and anything security-
sensitive in this session.
```

### Tools

**`delegate_to_local`** — starts a run, returns immediately.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `task` | yes | — | Self-contained task description. The local sub-session starts with no memory of the parent conversation. |
| `allowed_tools` | no | `Read,Grep,Glob` (read-only) | Comma-separated tools the local sub-session may use without prompting. Widen to `Read,Edit,Write,Bash,Grep,Glob` for tasks that need to write files or run commands. |
| `cwd` | no | server's cwd | Working directory for the local sub-session. |
| `resume_session_id` | no | — | Continue a prior run's local sub-session (its `session_id`, from `get_delegate_result`) instead of starting fresh. |
| `bare` | no | `false` | Run with `claude --bare` to cut a large fixed per-call token overhead, at the cost of dropping project skills/plugins/hooks. See "Two different token budgets" above. CLAUDE.md is always re-added even when `true`. |

Returns a `run_id`.

**`check_delegate_status`** — `{run_id}` → status (`running` / `completed` /
`error`) plus a compact progress summary while it's still going, plus any
pending questions the run has asked via `ask_parent`.

**`get_delegate_result`** — `{run_id}` → the final answer (with any
`<think>...</think>` block stripped defensively, in case a local model
leaks reasoning despite the settings that are supposed to suppress it),
local `session_id`, output tokens + tok/s, and cost estimate. Errors if the
run hasn't finished yet.

**`reply_to_delegate`** — `{run_id, message_id, answer}` → answers a pending
question surfaced by `check_delegate_status`.

The delegated run itself gets two more tools automatically (you never call
these from the parent session): **`ask_parent`** (`{question}` → `message_id`)
and **`check_message_status`** (`{message_id}` → the answer, once given).

### Run artifacts

Each run gets `~/.claude-local-delegate/runs/<run_id>/` containing
`prompt.md` (the task text), `output.log` (raw `claude -p` stream-json
output), `meta.json`, `exit_code` once finished, `mcp-config.json` (the
generated config that wires up `ask_parent` for this run), and a `messages/`
subdirectory with one JSON file per `ask_parent` question — useful for
debugging a run that didn't do what you expected. Override the location with
`CLAUDE_LOCAL_DELEGATE_RUNS_DIR`. Nothing prunes this directory automatically
yet; clean it out periodically if you delegate a lot.

### Cost estimate caveat

The `est. cost` in `get_delegate_result` is Claude Code's own client-side
estimate, computed as if the tokens were billed at Anthropic API rates. It is
**not real money** when the local model is genuinely free to run — treat it
only as a rough token-volume signal, not an actual bill.

## Safety note

The local sub-session runs *unsupervised* with whatever `allowed_tools` you
grant it — there's no human approving each tool call the way there is in an
interactive session. Keep the default read-only scope unless a task
genuinely needs write/exec access, and don't delegate tasks that touch
secrets, production systems, or anything you wouldn't want an unattended
process doing on your machine. Nothing currently kills a run that hangs or
runs long — check `check_delegate_status`/the log if one seems stuck, and
kill the `bash -c` wrapper PID (in `meta.json`) manually if needed.

## Testing manually

The server speaks newline-delimited JSON-RPC 2.0 (MCP stdio transport) on
stdin/stdout, so you can drive it by hand without Claude Code:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"delegate_to_local","arguments":{"task":"say hi","allowed_tools":"Read"}}}' \
  | python3 server.py
# -> prints a run_id; each tool call is its own process, so check status/
# result with a separate invocation once the background run has had time
# to progress:
echo '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"check_delegate_status","arguments":{"run_id":"<run_id>"}}}' \
  | python3 server.py
```

## Possible future work

- **Auto-expiring runs directory** — nothing prunes `~/.claude-local-delegate/runs/`
  yet.
- **Configurable timeout / auto-kill** for runs that hang.
- **Notice pending questions proactively** — right now the parent only
  learns about a pending `ask_parent` question by calling
  `check_delegate_status`; there's no push/interrupt.
- **Multi-model routing** (à la [Sakana Fugu](https://arxiv.org/html/2606.21228v1),
  a model orchestrator that picks the right model per query): not
  implemented, and not really applicable here yet -- this tool talks to
  exactly one local model (whatever `--settings` points at). It'd become
  relevant if you ran more than one local model side by side (e.g. a small
  fast one and a bigger one) and wanted `delegate_to_local` to pick between
  them by task shape, the way [houtini-ai/lm](https://github.com/houtini-ai/lm)
  does with a scored `bestTaskTypes` match against `/v1/models`.

## Ideas looked at and deliberately not taken

- **Algorithmic tool-output compression** (BM25/FTS indexing of raw tool
  output instead of dumping it into context, as in the "Context Mode" MCP
  server) — solves a different problem (built-in tool output bloat, e.g.
  `curl`/`kubectl`) than what this tool does; `check_delegate_status`
  already avoids the equivalent problem here by summarizing structured
  stream-json events rather than indexing free text.
- **Per-model prompt tuning / SQLite model-metadata cache** (from
  houtini-ai/lm) — real technique for juggling many differently-behaved
  local models; not relevant with a single fixed local backend.

Design for `ask_parent` borrowed from
[dvcrn/mcp-server-subagent](https://github.com/dvcrn/mcp-server-subagent).
Think-block stripping and the "measure real token/tok-s numbers" instinct
borrowed from [houtini-ai/lm](https://github.com/houtini-ai/lm).

## License

MIT
