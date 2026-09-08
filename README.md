# claude-local-delegate

An MCP server for Claude Code with tools to hand a task to a locally-hosted
model (e.g. served by vLLM) while keeping your main session on your regular
subscription/model. Every delegation is a **native Claude Code background
agent** (`claude --bg` in the agent-view system) pointed at your local
backend via `--settings` — so the delegated work is a real, first-class,
inspectable Claude Code session, not a bespoke "delegation entity" this
server invented on its own.

Parent-side tools: `delegate_to_local`, `check_delegate_status`,
`get_delegate_result`, `watch_delegate`, `stop_delegate`, `delegate_verified`
(work → local-checker → revise loop), `check_verified_status`,
`get_verified_result`, `fan_out_to_local`,
`check_fanout_status`, `get_fanout_result`.

## 0.8 — cheaper results, enforced least-privilege, and spawn provenance

v0.8.0 makes supervision cheaper and the defaults honest. The headline changes:

- **Result compaction.** `get_delegate_result` returns the first 4 + last 40
  lines of the agent's final answer plus a `sha256` of the full text, instead of
  the whole thing. `full: true` returns everything; `max_lines` sets the tail.
  `get_fanout_result` compacts **each** item to its last 15 lines the same way,
  with the same params. Defaults come from `CLAUDE_LOCAL_DELEGATE_RESULT_LINES`
  (40) and `CLAUDE_LOCAL_DELEGATE_FANOUT_LINES` (15). Nothing on disk is
  truncated — the transcript is intact and `full: true` still returns all of it.
- **Real least-privilege for read-only delegates.** A delegation whose
  `allowed_tools` are only read-only (Read, Grep, Glob, NotebookRead, TodoWrite)
  now spawns with `--permission-mode dontAsk` instead of `bypassPermissions`,
  because `bypassPermissions` **ignores** `--allowedTools`
  (anthropics/claude-code#12232) — so the allowlist is finally enforced rather
  than advisory. Delegations that can write still default to `bypassPermissions`
  (an unattended agent that prompts is an agent that hangs). Override:
  `CLAUDE_LOCAL_DELEGATE_READONLY_PERMISSION_MODE`.
- **Routing provenance.** Every spawn is recorded in
  `~/.claude-local-delegate/runs.json`: model, base URL, settings-file sha,
  permission mode, allowed tools, cwd, name, timestamp. `check_delegate_status`
  prints a `backend:` line from that record, and says plainly when a session was
  **not** spawned by this server.
- **Pool accounting fixed.** The local concurrency ceiling
  (`CLAUDE_LOCAL_DELEGATE_MAX_CONCURRENCY`, default 16) now counts only
  background sessions recorded as local delegates, plus in-flight spawns.
  Previously **any** `claude agents` background session — including a paid
  Anthropic supervisor session — occupied a slot in the local vLLM pool it never
  touched.
- **Cheaper status polls.** The transcript JSONL is now parsed **once** per poll
  instead of twice (`_transcript_summary`), and the result is memoised on
  (path, size, mtime), so polling an idle or finished agent costs nothing.
- **Lock contention gone.** Read-only MCP calls
  (`project_sync`, `local_backend_info`, `check_delegate_status`,
  `watch_delegate`, `get_delegate_result`, `check_fanout_status`,
  `get_fanout_result`) no longer take the cross-process operations flock, and
  `delegate_to_local` / `fan_out_to_local` / `continue_delegate` release it while
  the `claude --bg` subprocess starts (up to 120 s), holding their pool slot with
  an in-flight ticket file (`~/.claude-local-delegate/inflight.json`) instead.
  Previously one spawn froze every other supervisor's reads and spawns for up to
  two minutes.
- **Conditional verifier.** In `delegate_verified`, if a run has **no
  `acceptance_criteria`** and either the delegation was read-only or
  `git status --porcelain` in its cwd is clean, no checker is spawned at all — the
  run returns the worker's answer marked **"UNVERIFIED PASS"** with the reason.
  New `always_verify` forces the checker anyway. When the checker **does** run,
  its prompt now carries `git status --porcelain` + `git diff --stat HEAD` (real
  evidence) plus only the last 30 lines of the worker's self-report, instead of
  the entire self-report — so the checker's context no longer grows with the size
  of the worker's output.
- **Concurrency benchmark.** `contrib/bench_concurrency.py` is a standalone
  driver that speaks to this server over stdio and measures fan-out concurrency
  scaling (levels 1/2/4/8) into CSV.
- **Codex tool-output caps.** Codex clients can cap a tool's output per tool via
  `[mcp_servers.claude-local-delegate.tools.<tool>] output_token_limit = N` in
  `~/.codex/config.toml`; `contrib/install_codex.py` writes those blocks for fresh
  installs.

## 0.7 — Claude Code + Codex support

v0.7 lets both Claude Code and Codex share one local backend, coordinated in
[COORDINATION.md](COORDINATION.md). Your **main sessions** still start as plain
`claude` or `codex` — nothing about launching them changes. A single MCP
registration (added via `python3 contrib/install_codex.py --apply`) reuses the
existing local vLLM profile **for child processes only**, so workers and
checkers run on the local model even when their parent is Codex. No second
model profile or duplicated secrets. The installer also writes per-tool
`[mcp_servers.claude-local-delegate.tools.<tool>] output_token_limit = N`
blocks into `~/.codex/config.toml`, so a Codex client caps each tool's output
on its side too (the server-side compaction above is the first of the two).

Both clients work off the **same task reservations** in
`~/.claude-local-delegate/coordination.sqlite3`: claim a task before delegating
and never overlap a write. Because Codex has no native `SendMessage`, it talks
to a settled worker with the portable **`continue_delegate`** (fork/restart,
transcript retained); Claude keeps native `SendMessage`. Coordination is
**cooperative, not OS-enforced** — reservations are checked before spawning,
not filesystem locks, so for strong isolation use separate worktrees and a
single integration owner.

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
`--permission-mode`. The default is **split by whether the allowlist is
read-only**:

- A delegation whose `allowed_tools` are only read-only (Read, Grep, Glob,
  NotebookRead, TodoWrite) runs in `dontAsk` (override
  `CLAUDE_LOCAL_DELEGATE_READONLY_PERMISSION_MODE`). This is where the
  `--allowedTools` list is actually **enforced**: `dontAsk` denies unlisted
  tools instead of prompting for them. That matters because
  `bypassPermissions` **ignores `--allowedTools`**
  (anthropics/claude-code#12232) — a "read-only" delegate spawned in bypass
  could still run anything it asked for, so the allowlist used to be advisory
  rather than binding.
- A delegation that can write (or that you widen with `Bash`) defaults to
  `bypassPermissions` (override `CLAUDE_LOCAL_DELEGATE_PERMISSION_MODE`): an
  unattended agent that prompts on its first gated tool would park forever, so
  writers keep the full granted toolset unattended. This is an autonomous loop
  with no approval gates — don't point one at secrets or anything you wouldn't
  want an unattended agent doing on this machine.

Narrow a specific delegation with `permission_mode: "acceptEdits"` (Bash gated),
`"default"` (everything prompts), or `"auto"` (classifier-gated) when a task
should not have an unattended shell.

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

## Supervising a delegated run (watch → stop → re-delegate)

A delegated agent is unattended, but you should still **watch it like a human
watching the agent-view panel** — read what it *says*, not what it *does*, so
following a run stays cheap in paid tokens.

**The loop that works:** `watch_delegate` → `stop_delegate` → `delegate_to_local`
again. A *running* local `claude --bg` agent does **not** read a mid-run
`SendMessage` — it finishes its current run first, and a fast local model
usually finishes before the message is looked at; once it is `done` it is
unreachable by `SendMessage`. So when a run drifts, stop it and re-delegate a
sharper task rather than trying to steer it live. `SendMessage` is reliable only
for answering an agent that is **`blocked`** on its own question.

- **Every delegation is spawned with a supervision preamble** (`announce_plan`,
  on by default): the agent must post a numbered plan as its first message,
  then one plain sentence of intent before each step and one of outcome after —
  no pasted code, no file dumps. A mid-run message is treated as a
  course-correction. Turn it off per call with `announce_plan: false` for a
  trivial one-shot.
- **`watch_delegate(run_id)`** — the token-cheap view: the agent's *original
  task* + its plain-text narration (plan + per-step sentences) with **all tool
  output, file contents and code stripped**, plus output tokens burned so far.
  Call it repeatedly to follow a run. `check_delegate_status` now also shows the
  task, the agent's last sentence, and tokens-so-far in one compact read.
- **`stop_delegate(run_id, mode)`** — when the run is drifting. `mode:
  "interrupt"` (default, SIGINT) asks it to drop the current step; `mode:
  "terminate"` (SIGTERM) ends the run. The agent settles to `done` in ~10–15 s,
  before its next step. Native equivalent: the `TaskStop` tool with the agent's
  name.
- **Course-correct** — after stopping, read what the agent managed with
  `get_delegate_result(run_id)`, then `delegate_to_local` again with a smaller,
  sharper task, carrying any useful partial result forward in the new task text.
  A stopped agent is `done` and not reachable by `SendMessage`.
- **Answer a `blocked` agent** — the one case for `SendMessage`: when
  `check_delegate_status` / `watch_delegate` shows state `blocked`, the agent
  asked its own question; reply with the native `SendMessage` tool. (If the
  parent session's permission-mode class differs from the delegate's — e.g.
  parent `auto`, delegate default `bypassPermissions` — that message is held for
  a one-time user approval.)

**Prefer many small delegations over one long run.** A task you can state as a
single outcome is easy to watch and cheap to redo if it drifts; a sprawling one
is neither. Split big work and delegate the pieces (or `fan_out_to_local`). The
supervision preamble also tells the agent to flag, in its plan, when a task is
bigger than ~5 steps instead of silently doing all of it.

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

## Keeping the local model loaded (optional, recommended)

Two machine-level pieces (they live in `~/.claude/`, not in this repo — see
`contrib/`) make the supervision loop automatic so you never have to tell the
session "check what the agents are doing" or remind it the local model exists:

1. **`~/.claude/settings.json`** — add:

   ```json
   {
     "worktree": { "bgIsolation": "none" },
     "hooks": {
       "UserPromptSubmit": [
         { "hooks": [ { "type": "command",
           "command": "python3 /home/YOU/.claude/hooks/delegate-pool-status.py",
           "timeout": 25 } ] }
       ]
     }
   }
   ```

   - `worktree.bgIsolation: "none"` is **required for delegated agents to write
     files**: Claude Code otherwise blocks `Write`/`Edit` in the main checkout
     for background sessions until they call `EnterWorktree`, and an unattended
     agent burns its turns detouring into a worktree instead of doing the task.
   - The `UserPromptSubmit` hook runs `contrib/delegate-pool-status.py` on every
     turn and injects a one-line status: how many delegates are running, which
     are `blocked` (need an answer), and — when fewer than
     `CLAUDE_DELEGATE_TARGET` (default 3) are running — a reminder to split off
     mechanical work and `delegate_to_local` before doing it in-session.

2. **`~/.claude/hooks/delegate-pool-status.py`** — copy it from `contrib/`:

   ```bash
   mkdir -p ~/.claude/hooks
   cp contrib/delegate-pool-status.py ~/.claude/hooks/
   ```

   Then open `/hooks` once (or restart) so the settings watcher picks it up.
   3 is the throughput/latency knee on a single-GPU vLLM box (2->3 is +42%
   aggregate tok/s; 3->4 is +6%); raise `CLAUDE_DELEGATE_TARGET` only for
   unattended batch runs where per-item latency does not matter.

## Migrating to another machine

The repo carries the MCP code and `contrib/`. Everything else is per-machine:

1. `git pull` in this repo, then **restart Claude Code** (the MCP subprocess is
   spawned at session start — it does not hot-reload `server.py`).
2. Re-apply the two `~/.claude/` pieces from the section above (settings block +
   `contrib/delegate-pool-status.py` -> `~/.claude/hooks/`), fixing the hook
   path to that machine's home.
3. Already needed by every version, so likely already present on a machine that
   ran the old one: `~/.claude/vllm.delegate.settings.json` (the local backend
   profile — **contains secrets**, copy it out of band, never commit it),
   `~/.claude/agents/local-worker.md`, the MCP registration above, and network
   reach to the vLLM box in `ANTHROPIC_BASE_URL`.

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
| `permission_mode` | no | `dontAsk` if read-only, else `bypassPermissions` | Native `--permission-mode`. A read-only allowlist defaults to `dontAsk` so `--allowedTools` is actually enforced (bypass **ignores** the allowlist — anthropics/claude-code#12232); anything wider defaults to `bypassPermissions` so the unattended agent runs its full granted toolset (Bash included) without prompting and parking. Narrow with `acceptEdits` (Bash gated) / `default` (all prompts) / `auto` (classifier-gated). |
| `disallowed_tools` | no | — | Comma-separated tools to strip (e.g. `Bash`) — forces the agent onto a path it can finish when a user-level hook blocks a tool it would reach for. |
| `agent` | no | `local-worker` | Subagent persona (system prompt). `""` = no persona. |
| `announce_plan` | no | `true` | Prepend the supervision preamble: agent posts a numbered plan first, then one plain sentence before/after each step (what `watch_delegate` surfaces). `false` for a trivial one-shot. |

Returns the agent's **native id** (the id you see in `claude agents`).

**`watch_delegate`** — `{run_id, max_lines?}` → token-cheap supervision view:
the agent's original task + its plain-text narration (numbered plan, one
sentence of intent per step, one of outcome), with **all tool output and code
stripped**, plus output tokens burned. `max_lines` tails the narration
(default 48; `<=0` for the whole run). Call repeatedly to follow a run.

**`stop_delegate`** — `{run_id, mode?}` → halt a drifting agent by signalling
its process. `mode: "interrupt"` (default, SIGINT) / `"terminate"` (SIGTERM).
The agent settles to `done` in ~10–15 s; it is then not reachable by
`SendMessage`, so course-correct by calling `delegate_to_local` again with a
sharper task (`get_delegate_result` still returns the stopped run's transcript).
Native equivalent: `TaskStop` with the agent name.

**`check_delegate_status`** — `{run_id}` → the agent's **native state**
(`working` / `blocked` / `completed` / `failed` / `stopped`) plus its cwd and
full session id. When the state is `blocked`, the agent's last words (its
question) are printed so you can answer with the native `SendMessage` tool.
Cheap: reads `claude agents --json`, does not touch the model.

**`get_delegate_result`** — `{run_id, full?, max_lines?}` → the agent's final
answer, read from its **native transcript** (`~/.claude/projects/<dir>/<sessionId>.jsonl`).
Compacted by default: the first 4 + last 40 lines plus a `sha256` of the full
text (so a long answer can't flood the parent's context); `full: true` returns
everything and `max_lines` overrides the tail size
(`CLAUDE_LOCAL_DELEGATE_RESULT_LINES`, default 40). Nothing on disk is
truncated — the transcript is intact. Errors while the agent is still
`working`/`blocked` — call `check_delegate_status` first. **Review the output
before trusting it** — see the A/B test above for why a local-model agent can
look right while being subtly wrong.

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

**`get_fanout_result`** — `{batch_id, full?, max_lines?}` → every item's final
answer, read from each agent's native transcript, concatenated. Each item is
compacted to its last 15 lines plus a `sha256` by default
(`CLAUDE_LOCAL_DELEGATE_FANOUT_LINES`, default 15) — N full answers in one tool
result is the single biggest context cost of a fan-out; `full: true` returns
each item whole, and `max_lines` overrides the per-item tail. Errors until all
agents are settled. Synthesize yourself (or delegate the synthesis to one more
`delegate_to_local` run if you want it done by the model).

### Verified delegation (`delegate_verified`)

For a task with a **checkable outcome** (code that must build / pass tests /
meet stated criteria), `delegate_verified` runs a closed **work → independent
check → revise** loop *entirely on the local model*, and hands the parent back
only a result a checker has already signed off on — or a failure report with
the checker's reasons. The point is that the parent session never has to review
raw local-model output, or remember it has a delegate in flight.

One worker `claude --bg` agent does the task. Then a **checker** agent — the
`local-checker` persona, with **no Edit/Write tools by design** — re-verifies
against the real working tree: reads the diff, builds, runs tests/lint, and
ends its message with `VERDICT: PASS` or `VERDICT: FAIL` plus concrete reasons.
On `FAIL` those reasons are prepended to a fresh worker round.

It is a lazily-advanced state machine, same pattern as fan-out batches: state
lives in `~/.claude-local-delegate/verified/<vid>.json`, and each
`check_verified_status` call moves the run **at most one step** by reading the
sub-agents' native state. Nothing blocks the single-threaded MCP loop; the
parent's polling cadence drives it.

**`delegate_verified`** — returns a `vid` immediately.

| Parameter | Required | Default | Description |
| :-- | :-- | :-- | :-- |
| `task` | yes | — | Self-contained task for the worker. Exact paths, and what "done" means. |
| `acceptance_criteria` | no | — | Explicit pass/fail conditions the checker must confirm (`pytest -q` green, `ruff check` passes, CLI prints X for input Y). **Strongly recommended** — without it the checker only derives its own best-guess checks and the gate is weak. |
| `allowed_tools` | no | `Read,Grep,Glob,Edit,Write,Bash` | Tools for the **worker** (it must be able to change code). The checker's tools are fixed at `Read,Grep,Glob,Bash` — no Edit/Write. |
| `cwd` | no | server's cwd | Working directory for both worker and checker. |
| `always_verify` | no | `false` | Force the checker round even when there is nothing objective to verify (see "Conditional verification" below). |
| `max_iterations` | no | `3` (`CLAUDE_LOCAL_DELEGATE_MAX_VERIFY_ITERS`) | Max work→check rounds, clamped 1..10. |
| `timeout_seconds` | no | `5400` (`CLAUDE_LOCAL_DELEGATE_VERIFY_TIMEOUT`) | Wall-clock ceiling for the whole loop; past it the run is force-failed. |

Runaway-loop guards: iteration cap, **stagnation detection** (worker emits a
byte-identical result two rounds running → stop), and the wall-clock ceiling.

**`check_verified_status`** — `{vid}` → advances the loop one step and reports
the phase (`working` / `checking` / `passed` / `failed`), elapsed vs timeout,
iteration count, and a per-round trail (`worker id → checker id → verdict`).
Poll it the way you'd poll `check_delegate_status`.

**`get_verified_result`** — `{vid}` → errors (with the current phase) until the
loop settles. On **PASS**: the signed-off worker answer plus the verification
trail. On **FAIL**: the reason (iteration cap / stagnation / timeout / crashed
sub-agent), the last checker's feedback, and the last candidate answer (kept,
not discarded — the working tree still holds its changes).

**Conditional verification.** The checker is a full second local round-trip, so
it only runs when there is something objective to check. A run with **no
`acceptance_criteria`** AND (a read-only delegation, or a clean
`git status --porcelain` in its cwd) skips the checker entirely: the run returns
the worker's answer marked **"UNVERIFIED PASS"** with the reason — review that
one yourself, or re-run with `acceptance_criteria` (or `always_verify: true`) to
force an independent check. When the checker **does** run, its prompt carries
`git status --porcelain` + `git diff --stat HEAD` (evidence from the real tree)
plus only the last 30 lines of the worker's self-report, instead of the whole
self-report — so the checker's context stays flat regardless of how long the
worker's output is.

### Where the agents live (native, not a home-grown store)

There is no `~/.claude-local-delegate/runs/` anymore. Each delegated task is a
first-class background session owned by Claude Code's agent-view supervisor:

- **Roster / state**: `claude agents --json` (or the `claude agents` panel) —
  id, cwd, native `state`.
- **Transcript (the result)**: `~/.claude/projects/<sanitized-cwd>/<sessionId>.jsonl`
  — the same file `claude attach` / `claude logs` read.
- **This server's on-disk artifacts** are `~/.claude-local-delegate/batches/<batch_id>.json`,
  which maps a fan-out `batch_id` to the native agent ids it spawned so
  `check_fanout_status` / `get_fanout_result` can aggregate them,
  `~/.claude-local-delegate/verified/<vid>.json`, the state machine for a
  `delegate_verified` run (spec, phase, per-iteration worker/checker ids and
  verdicts, final answer), `~/.claude-local-delegate/runs.json`, the per-spawn
  routing provenance (model, base URL, settings sha, permission mode, allowed
  tools, cwd, name, timestamp — the source of `check_delegate_status`'s
  `backend:` line and the pool accounting), and
  `~/.claude-local-delegate/inflight.json`, the in-flight spawn tickets that
  hold a pool slot while a `claude --bg` subprocess starts. All small JSON;
  nothing to prune by hand.

To inspect or drive an agent directly, no MCP needed: `claude attach <id>`,
`claude logs <id>` — or, from within a Claude Code session, the native
`SendMessage` / `ListAgents` / `TaskStop` tools, or this server's
`watch_delegate` / `stop_delegate`.

### Cost / token notes

`get_delegate_result` returns the agent's **native transcript** text, so there
is no separate cost or tok/s figure here — those live in the agent's own
session (visible via `claude attach <id>` or its transcript). When the local
model is genuinely free to run, treat any client-side estimate as a rough
token-volume signal, not a bill. The answer handed to the parent is compacted
by default (head + tail + `sha256`; `full: true` for everything), so the cost
you actually pay in the supervising session is bounded regardless of how long
the local agent's final answer is.

## Safety note

The delegated agent runs *unsupervised* under whatever `--permission-mode`
you give it. The default is split: a **read-only** delegation (allowlist of
only Read/Grep/Glob/NotebookRead/TodoWrite) runs in `dontAsk`, where the
allowlist is genuinely **enforced** (unlisted tools are denied, not
prompted) — so a "read-only" delegate cannot silently gain shell. A delegation
that can **write** defaults to `bypassPermissions`, an autonomous loop with no
approval gates: don't point one at secrets, production systems, or anything
you wouldn't want an unattended agent doing on this machine.

This server deliberately does **not** let one AI session approve another
AI session's blocked action. A delegated agent that gets gated shows its
native `blocked` state; you (the human) unblock it — with `--permission-mode`
at spawn, or by replying through the native `SendMessage` tool if it asks a
question. That keeps a human in the loop exactly where it matters.

To stop a delegated agent that hangs, runs long, or drifts off task, use
`stop_delegate(run_id)` (signals the agent's process — `interrupt`/`terminate`),
the native `TaskStop` tool with its name, or `claude attach <id>` then
interrupt. It settles to `done` in ~10–15 s; to then point it a different way,
`delegate_to_local` again with a sharper task (re-use its transcript via
`get_delegate_result`). A running agent won't read a mid-run `SendMessage`, and
a stopped one is unreachable by it — reserve `SendMessage` for answering a
`blocked` agent.

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
