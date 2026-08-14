# claude-local-delegate

An MCP server for Claude Code with three tools — `delegate_to_local`,
`check_delegate_status`, `get_delegate_result` — that hand a task to a
locally-hosted model (e.g. served by vLLM) while keeping your main session on
your regular subscription/model.

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

Returns a `run_id`.

**`check_delegate_status`** — `{run_id}` → status (`running` / `completed` /
`error`) plus a tail of the run's log while it's still going.

**`get_delegate_result`** — `{run_id}` → the final answer, local
`session_id`, and cost estimate. Errors if the run hasn't finished yet.

### Run artifacts

Each run gets `~/.claude-local-delegate/runs/<run_id>/` containing
`prompt.md` (the task text), `output.log` (raw `claude -p` stream-json
output), `meta.json`, and `exit_code` once finished — useful for debugging a
run that didn't do what you expected. Override the location with
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

- **Bi-directional communication**: let a delegated run ask the parent
  session a clarifying question mid-task instead of guessing or failing,
  the way [dvcrn/mcp-server-subagent](https://github.com/dvcrn/mcp-server-subagent)
  does with `ask_parent`/`reply_subagent`. Would need the child `claude -p`
  process to have its own way to reach back to the parent (e.g. via
  `--mcp-config` pointed at a small IPC channel) — meaningfully more complex
  than the polling model here, not implemented.
- **Auto-expiring runs directory** — nothing prunes `~/.claude-local-delegate/runs/`
  yet.
- **Configurable timeout / auto-kill** for runs that hang.

## License

MIT
