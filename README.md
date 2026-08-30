# claude-local-delegate

An MCP server for Claude Code with tools to hand a task to a locally-hosted
model (e.g. served by vLLM) while keeping your main session on your regular
subscription/model. Every delegation is a **native Claude Code background
agent** (`claude --bg` in the agent-view system) pointed at your local
backend via `--settings` — so the delegated work is a real, first-class,
inspectable Claude Code session, not a bespoke "delegation entity" this
server invented on its own.

Parent-side tools: `delegate_to_local`, `check_delegate_status`,
`get_delegate_result`, `fan_out_to_local`, `check_fanout_status`,
`get_fanout_result`.

## Why a native `claude --bg` agent instead of a `claude -p` black box

The v0.3 design spawned a headless `claude -p` subprocess with its own
`run_id`, its own file-based `ask_parent`/`check_message_status` protocol, and
a second `server.py` process in "child" mode. That worked, but the delegated
unit was opaque: invisible in `claude agents`, un-attachable, and coordinated
through this server's own machinery.

v0.4 keeps the same public tool surface but makes each delegation a **native
background agent**. Concretely, `delegate_to_local` runs
`claude --bg --name <slug> --settings <vllm profile> --permission-mode
<mode> --allowedTools <tools> <task>` and hands back the agent's **native id**.
That id is the id you see in `claude agents`:

- **Visible & inspectable**: `claude logs <id>`, `claude attach <id>` work on
  it directly, no MCP in the loop.
- **Status** (`check_delegate_status`) reads the agent's **native** state
  (`working` / `blocked` / `completed` / `failed` / `stopped`) from
  `claude agents --json` — no home-grown `exit_code` bookkeeping.
- **Result** (`get_delegate_result`) reads the agent's **native transcript**
  (`~/.claude/projects/<dir>/<sessionId>.jsonl`) and returns its final answer.
- **Talking to it** is the **native** cross-session machinery: the parent
  session reaches the agent with `SendMessage` (it shows up in `ListAgents`),
  and the agent's own `blocked` state is the native "I need input" signal.
  There is no more `ask_parent`/`check_message_status`/child-process protocol.

This is the point of the rework: the local model rides on the SAME native
agent machinery your main session already uses. One mechanism, one set of
ids, one place to look. The MCP is just the spawner and the reader.

### `--permission-mode` (required for unattended delegation)

A native `claude --bg` session starts in **manual mode**, where `--allowedTools`
does *not* auto-approve (unlike headless `claude -p`): the agent would block
on its first gated tool with no human present. So the spawner always passes a
`--permission-mode`. The default is `acceptEdits` — read/edit/write run
unattended, Bash stays gated — which is the safe analogue of the old
read-only/write-only delegations. Pass `permission_mode: "bypassPermissions"`
to a delegation only when it genuinely needs unattended shell access and you
accept an autonomous loop with no approval gates (the auto-mode safety
classifier may itself object to that flag, which is the correct behavior).

## Why this instead of a prompt-wrapper MCP tool

`delegate_to_local` doesn't summarize context into a single completion
request. It launches a real Claude Code agent with `--settings` pointed at your
local backend. The delegated task runs through the actual Claude Code agent
loop — its own tool calls (Read/Edit/Bash/...), its own context management, its
own `CLAUDE.md` — just backed by a different model. From the repo's point of
view it *is* Claude Code, not an approximation of it.

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

## The delegated agent can ask back (natively)

Because each delegation is a real native background agent, it does not need a
custom ask-back protocol. When a delegated agent gets stuck on something only
the parent session can decide, it surfaces Claude Code's own **`blocked`**
state in `claude agents`. The parent reads that state through
`check_delegate_status` (which also prints the agent's last words, its
question) and answers with the **native `SendMessage` tool** addressed to the
agent — the same cross-session messaging your own sessions use. No
`ask_parent`/`check_message_status` tools, no second child-mode `server.py`
process, no message files.

This is load-bearing for the same reason as before: without it a delegated
agent that hits something it can't decide either guesses (silently, possibly
wrong) or fails outright. With it, it stops and the parent can unblock it.

## Two different token budgets, two different defaults

The local model's tokens are free (self-hosted); the parent session's tokens
are the paid ones. The defaults reflect that asymmetry rather than treating
"fewer tokens" as universally good:

- **Local side:** the delegated agent loads the full project context (CLAUDE.md,
  skills, MCP servers) the way any real session does — it costs the local model
  input tokens, which are free, and can only help accuracy on anything
  project-specific.
- **Parent side, `check_delegate_status`:** this *is* paid-token territory, so
  it reads the agent's **native state** (a single `claude agents --json` field)
  and, when `blocked`, the agent's last assistant line from its transcript — a
  compact, structured read — instead of dumping a raw log tail, which is mostly
  `system/init` noise (the full skill/tool catalog, thousands of tokens) for
  zero signal.

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
sensitive in this session. Always review a delegated result before treating
it as final -- a delegated run can produce plausible-looking but subtly
wrong output (e.g. a correct-looking config with an inverted sign) that only
a read-through catches; reviewing already-generated output is cheap
relative to what delegation saved.
```

### A/B test: same task, online vs delegated

Ran the same well-specified, self-contained coding task (a new IsaacLab
reward-term function, given identical reference examples) through Sonnet 5
directly and through `delegate_to_local`. Core logic came out equally
correct both times. The gap was elsewhere:

- **Config correctness**: the delegated run's registration example used the
  wrong sign on a weight parameter (`weight=1.0` instead of `-1e-3` for a
  penalty term) -- semantically inverted, would silently reward the thing
  it was supposed to penalize. Caught only by reading the output.
- **Instruction-following**: told to output only code, no prose, the
  delegated run added preamble and trailing commentary anyway.
- **Turn/token blowup from permission mismatches**: given `allowed_tools`
  too narrow for what it wanted to verify (it tried, reasonably, to check
  its answer against the real upstream source via Bash/WebSearch/WebFetch),
  the run burned 7 turns hitting denials before finishing -- ~197k
  cumulative input tokens vs. a ~31k single-turn baseline, because retries
  aren't cache-discounted the way they would be against the Anthropic API.
  This is what `check_delegate_status`'s blocked-call detector (below) is
  for -- it's a config/tooling mismatch, not evidence the local model can't
  do the task.

### Tools

**`delegate_to_local`** — spawns one native background agent, returns immediately.

| Parameter | Required | Default | Description |
|---|---|---|---|
| `task` | yes | — | Self-contained task description. The agent starts with no memory of the parent conversation. |
| `allowed_tools` | no | `Read,Grep,Glob` (read-only) | Comma-separated tools granted to the agent. Widen to `Read,Edit,Write` for tasks that write files. |
| `cwd` | no | server's cwd | Working directory for the agent. |
| `name` | no | slug of the task | Display name shown in `claude agents`. |
| `permission_mode` | no | `acceptEdits` | Native `--permission-mode`. `acceptEdits` = read/edit/write run unattended, Bash stays gated. Use `bypassPermissions` only for genuinely unattended-shell work. |

Returns the agent's **native id** (the id you see in `claude agents`).

**`check_delegate_status`** — `{run_id}` → the agent's **native state**
(`working` / `blocked` / `completed` / `failed` / `stopped`) plus its cwd and
full session id. When the state is `blocked`, the agent's last words (its
question) are printed so you can answer with the native `SendMessage` tool.
Cheap: reads `claude agents --json`, does not touch the model.

**`get_delegate_result`** — `{run_id}` → the agent's final answer, read from
its **native transcript** (`~/.claude/projects/<dir>/<sessionId>.jsonl`).
Errors while the agent is still `working`/`blocked` — call
`check_delegate_status` first. **Review the output before trusting it** — see
the A/B test above for why a local-model agent can look right while being
subtly wrong.

**`fan_out_to_local`** — `{items[], shared_instruction, allowed_tools?, cwd?, permission_mode?}`
→ map-reduce over the local model: spawns one native background agent per item,
all sharing `shared_instruction`. Returns a `batch_id` immediately (same async
pattern as everything else here). Why this beats stuffing everything into one
giant local context: each item fits comfortably in its own window, chunking
tends to beat one huge context on accuracy (long-context recall degrades the
more you cram in — "lost in the middle"), and the *parent* session never reads
the raw material, only the per-item answers.

Real ceiling to know about: parallel items share this box's fixed vLLM
concurrency (`--max-num-seqs`, default assumed 16 here — override via
`CLAUDE_LOCAL_DELEGATE_MAX_CONCURRENCY`) and, more importantly, its
VRAM/KV-cache pool. More items than that don't fail, they queue behind the
first batch — and if each item's context is itself large, several running
near-simultaneously contend for the same KV-cache, so parallelism doesn't scale
as cleanly as "more items = more parallel" implies. The tool warns when a batch
exceeds the configured ceiling; it does not cap batch size itself.

**`check_fanout_status`** — `{batch_id}` → native-state counts across the
batch (working / blocked / completed / failed / stopped) plus each agent's
state.

**`get_fanout_result`** — `{batch_id}` → every item's final answer, read from
each agent's native transcript, concatenated. Errors until all agents are
settled. Synthesize yourself (or delegate the synthesis to one more
`delegate_to_local` run if you want it done by the model).

### Where the agents live (native, not a home-grown store)

There is no `~/.claude-local-delegate/runs/` anymore. Each delegated task is a
first-class background session owned by Claude Code's agent-view supervisor:

- **Roster / state**: `claude agents --json` (or the `claude agents` panel) —
  id, cwd, native `state`.
- **Transcript (the result)**: `~/.claude/projects/<sanitized-cwd>/<sessionId>.jsonl`
  — the same file `claude attach` / `claude logs` read.
- **This server's only on-disk artifact** is `~/.claude-local-delegate/batches/<batch_id>.json`,
  which maps a fan-out `batch_id` to the native agent ids it spawned so
  `check_fanout_status` / `get_fanout_result` can aggregate them. Nothing
  else to prune.

To inspect or drive an agent directly, no MCP needed: `claude attach <id>`,
`claude logs <id>`, `claude stop <id>` — or, from within a Claude Code
session, the native `SendMessage`/`ListAgents` tools.<think>...</think>` block stripped defensively, in case a local model
leaks reasoning despite the settings that are supposed to suppress it),
local `session_id`, output tokens + tok/s, and cost estimate. Errors if the
### Cost / token notes

`get_delegate_result` returns the agent's **native transcript** text, so there
is no separate cost or tok/s figure here — those live in the agent's own
session (visible via `claude attach <id>` or its transcript). When the local
model is genuinely free to run, treat any client-side estimate as a rough
token-volume signal, not a bill.

## Safety note

The delegated agent runs *unsupervised* under whatever `--permission-mode`
you give it. The default `acceptEdits` lets it read/edit/write files without
per-call prompts but keeps Bash gated — a good, conservative analogue of the
old read-only/write-only delegations. Raise to `bypassPermissions` only for
work that genuinely needs unattended shell access, and accept that it is an
autonomous loop with no approval gates: don't point one at secrets,
production systems, or anything you wouldn't want an unattended agent doing on
this machine.

This server deliberately does **not** let one AI session approve another
AI session's blocked action. A delegated agent that gets gated shows its
native `blocked` state; you (the human) unblock it — with `--permission-mode`
at spawn, or by replying through the native `SendMessage` tool if it asks a
question. That keeps a human in the loop exactly where it matters.

To stop a delegated agent that hangs or runs long, use the native
`claude stop <id>` (or `claude attach <id>` then interrupt). There is no
home-grown wrapper PID to hunt down.

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

### Cold-start validation

The tool descriptions are meant to be sufficient on their own -- a fresh
Claude Code session, with no memory of how this server was built and no
`CLAUDE.md` guidance, should be able to use it correctly just from
`tools/list`. Verified this directly: ran two independent `claude -p`
sessions with only this server's `--mcp-config` and no other context.

- Task 1 (single delegation): correctly called `delegate_to_local` →
  `check_delegate_status` (polling) → `get_delegate_result`, noticed a
  blocked tool call mid-run and restarted with narrower scope on its own,
  and correctly distinguished `check_delegate_status` (status + log) from
  `get_delegate_result` (final answer) -- exactly matching each tool's
  description, not something it could have inferred from a hidden default.
- Task 2 (three independent facts to combine): correctly chose
  `fan_out_to_local` over three manual `delegate_to_local` calls, correctly
  sequenced `fan_out_to_local` → `check_fanout_status` → `get_fanout_result`,
  and cited the "review before trusting" line from `get_delegate_result`'s
  description verbatim as its next step.

No description gaps found in either run -- both sessions reasoned about the
right sequence, including the aggregation-becomes-a-normal-run detail,
purely from `tools/list` output.

## Possible future work

- **Auto-expire delegated agents** — native background sessions persist until
  the supervisor stops idle ones; a delegate-specific TTL/cleanup is not
  wired here (use `claude stop <id>` or `claude agents` to manage them).
- **Configurable timeout / auto-kill** for agents that hang.
- **Proactive "needs input" notice** — the parent learns an agent is `blocked`
  by calling `check_delegate_status` (the native state); Claude Code itself
  has no push to the parent session yet.
- **Multi-model routing** (à la [Sakana Fugu](https://arxiv.org/html/2606.21228v1),
  a model orchestrator that picks the right model per query): not
  implemented, and not really applicable here yet — this tool talks to exactly
  one local model (whatever `--settings` points at). It'd become relevant if
  you ran more than one local model side by side (e.g. a small fast one and a
  bigger one) and wanted `delegate_to_local` to pick between them by task
  shape, the way [houtini-ai/lm](https://github.com/houtini-ai/lm) does with a
  scored `bestTaskTypes` match against `/v1/models`.

## Why a native agent is the right answer to "delegate to a local model"

An in-session subagent (the `Agent`/`Task` tool, or agent-teams teammates)
**cannot** run against a different provider or base URL than its parent —
Claude Code has one `ANTHROPIC_BASE_URL` per session, and
`CLAUDE_CODE_SUBAGENT_MODEL` only swaps the model *id* within it. Your local
model sits at a *different* endpoint, so the only native way to run it is a
**separate top-level session** pointed there with `--settings`. That is exactly
what `claude --bg` (agent view) is: independent, supervised, panel-visible
Claude Code sessions. So rather than inventing a bespoke "delegation entity",
this server just spawns and observes those native background agents.

## Ideas looked at and deliberately not taken

- **Algorithmic tool-output compression** (BM25/FTS indexing of raw tool
  output instead of dumping it into context, as in the "Context Mode" MCP
  server) — solves a different problem (built-in tool output bloat, e.g.
  `curl`/`kubectl`) than what this tool does.
- **Per-model prompt tuning / SQLite model-metadata cache** (from
  houtini-ai/lm) — real technique for juggling many differently-behaved
  local models; not relevant with a single fixed local backend.

## License

MIT
