# claude-local-delegate

An MCP server for Claude Code with one tool, `delegate_to_local`, that hands a
task to a locally-hosted model (e.g. served by vLLM) while keeping your main
session on your regular subscription/model.

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

Restart Claude Code. The `delegate_to_local` tool is now available in every
session.

## Usage

Claude decides when to delegate based on the tool description (mechanical /
high-volume work: drafts, boilerplate, straightforward refactors,
summarization). You can also nudge it explicitly, or add a rule to your
`CLAUDE.md`:

```
For mechanical, high-volume, or low-risk work (boilerplate, drafts,
summaries, simple refactors), use the delegate_to_local tool instead of
doing it yourself. Keep architecture decisions and anything security-
sensitive in this session.
```

### Tool parameters

| Parameter | Required | Default | Description |
|---|---|---|---|
| `task` | yes | — | Self-contained task description. The local sub-session starts with no memory of the parent conversation. |
| `allowed_tools` | no | `Read,Grep,Glob` (read-only) | Comma-separated tools the local sub-session may use without prompting. Widen to `Read,Edit,Write,Bash,Grep,Glob` for tasks that need to write files or run commands. |
| `cwd` | no | server's cwd | Working directory for the local sub-session. |
| `timeout_s` | no | `300` | Max seconds to wait. |
| `resume_session_id` | no | — | Continue a prior `delegate_to_local` sub-session instead of starting fresh. |

### Cost estimate caveat

The `est. cost` in the tool's response is Claude Code's own client-side
estimate, computed as if the tokens were billed at Anthropic API rates. It is
**not real money** when the local model is genuinely free to run — treat it
only as a rough token-volume signal, not an actual bill.

## Safety note

The local sub-session runs *unsupervised* with whatever `allowed_tools` you
grant it — there's no human approving each tool call the way there is in an
interactive session. Keep the default read-only scope unless a task
genuinely needs write/exec access, and don't delegate tasks that touch
secrets, production systems, or anything you wouldn't want an unattended
process doing on your machine.

## Testing manually

The server speaks newline-delimited JSON-RPC 2.0 (MCP stdio transport) on
stdin/stdout, so you can drive it by hand without Claude Code:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"delegate_to_local","arguments":{"task":"say hi","allowed_tools":"Read"}}}' \
  | python3 server.py
```

## License

MIT
