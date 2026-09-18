# claude-local-delegate — architecture

How the pieces fit together, why each is shaped the way it is, and where the
state lives. The one-page overview is in `README.md`; the binding contract is
`REQUIREMENTS.md`; this file explains the structure.

## 1. The core idea

Delegation is a **native Claude Code background agent** (`claude --bg`) pointed
at a local model via `--settings`, not a bespoke "delegation entity" this
project invented. Every delegated run is therefore a first-class, inspectable
Claude Code session: visible in `claude agents`, with a real JSONL transcript on
disk, and resumable. The MCP servers in this repo are a thin control plane over
those native sessions — they spawn them, read their state/transcript, and
record planning + outcome data.

This is the single most important decision and the reason the code is "thin":
the heavy lifting (agent loop, tools, permission modes, transcripts) is done by
Claude Code itself, so this repo only has to orchestrate and observe.

## 2. Components

```
                    +--------------------------------------------------+
   supervisor       |  claude-local-delegate  (MCP, stdio)             |
   (Claude Code /   |  server.py  -- delegate_to_local, get_result,    |
    Codex)          |  check/watch/stop, fan_out, delegate_verified,   |
         |          |  continue, rate_delegate, project_sync/claim/    |
         | spawn/   |  update/note  + the coordination Board           |
         v inspect  +--------------------------------------------------+
   +--------------------------------------------------+        |
   | native claude --bg agent (local model via vLLM)   |        | reads/writes
   | real session in `claude agents`, JSONL transcript  |        v
   +--------------------------------------------------+   ~/.claude-local-delegate/
                                                           coordination.sqlite3
                                                           metrics.jsonl
                                                           runs.json, verified/, batches/

   code-nav  (MCP)  ->  code_nav_server.py -> code_nav.py
        a repository-map ROUTER only (selects child maps/paths from
        docs/design/repository_map.yaml). Semantic navigation is owned by
        Serena (a separate MCP), granted to read-only delegates.
```

| Module | Role | Notes |
|---|---|---|
| `server.py` | The delegation MCP. Spawns `claude --bg`, reads roster + transcripts, runs the verified loop, fan-out, and the coordination handlers. | Largest module. Spawn path is `_spawn_native_agent`; roster is `claude agents --json`; transcript parsing is `_transcript_summary` (one cached pass). |
| `coordination.py` | The cross-client task **Board** (SQLite). `claim`/`update`/`sync`/`note`/`authorize`. | Reservations are cooperative (never block); `BEGIN IMMEDIATE` arbitrates independent MCPs. State + append-only `events`. |
| `coordination_runtime.py` | Wires the Board into `server.py`: the operation lock, pool tickets, `spawn`, `install`. | Keeps `server.py` from growing a second coordination path. |
| `metrics.py` | The **shared analytics core**: `append_event` (the ledger writer) and the readers (`load_ledger`, `load_verified`, `load_batches`, `percentile`, `parse_timestamp`) + `transcript_stats`. | Single source of truth for the ledger/verified/batch formats. Never breaks a spawn. |
| `local_backend.py` | Loads the settings profile and the vLLM route **without leaking credentials** into diagnostics. | |
| `code_nav.py` / `code_nav_server.py` | Repository-map router (one MCP tool). No literal search, no symbol index. | Kept separate from the delegation MCP so a delegated agent (denied the recursive delegation MCP) still gets navigation. |

## 3. The delegation lifecycle

1. **Plan** — supervisor calls `project_sync` then `task_claim` (a write
   reservation with literal paths). `complexity`/`est_minutes`/`blocks`/`profile`
   are recorded for scheduling + metrics.
2. **Spawn** — `delegate_to_local` → `_spawn_native_agent` builds the
   `claude --bg` command: `--settings` (local gateway), `--tools` (only the
   granted built-ins, so the fixed tool-schema overhead stays small),
   `--allowedTools`/`--disallowedTools` (least privilege; the recursive
   delegation MCP is always disallowed), `--agent` (persona), `--permission-mode`
   (`bypassPermissions` for unattended writers, `dontAsk` for read-only).
3. **Wait** — the normal path is one `get_delegate_result(run_id,
   wait_seconds=900)`: the server waits server-side and returns the compact
   answer in a single parent turn (no paid status polling). `check_status` /
   `watch_delegate` are diagnostic only.
4. **Classify** — on `blocked`, `_classify_blocked` reads the roster `waitingFor`
   plus the transcript tail to name the cause (hook-gate, mcp-tool-missing,
   websearch-broken, permission-prompt, needs-input, unknown). One `blocked`
   event is persisted to the ledger (per episode, not per poll) so analytics can
   aggregate it.
5. **Rate** — after the supervisor reviews the result, `rate_delegate` writes a
   `rate` event (two 0–100 scores + transcript stats). This is the raw material
   of the quality loop.
6. **Close** — `task_update(status="done", …)` + incremental `project_sync`.

The **verified loop** (`delegate_verified` / `check_verified_status`) is the same
pattern made objective: a local worker runs, a read-only checker verifies against
explicit acceptance criteria, and the worker revises on failure (bounded
iterations). Each phase is itself a native agent; state lives in `verified/*.json`.

## 4. State on disk

All state is local, file-based, and readable without the server running:

| File | Format | Written by |
|---|---|---|
| `coordination.sqlite3` → `tasks`, `events` | SQLite; `tasks.body` is a JSON task (status, paths, runs[], note); `events` is an append-only log | `coordination.py` |
| `metrics.jsonl` | append-only JSONL: `spawn`, `rate`, `blocked` events | `server.py` via `metrics.append_event` |
| `runs.json` | run_id → provenance (backend, model, settings fingerprint, tools, …) | `server.py` |
| `verified/*.json` | one file per verified cycle (phase, iteration, history, final_answer) | `server.py` |
| `batches/*.json` | one file per fan-out batch (agent_ids, shared_instruction) | `server.py` |
| `session-starts.json`, `inflight.json`, `gateway-status.json` | cohort / pool tickets / gateway route | `server.py` |

The transcripts themselves live where Claude Code puts them
(`~/.claude/projects/<sanitized-cwd>/<session>.jsonl`); this repo never re-
stores them — the analytics stream them read-only.

## 5. Why not a framework

Considered and rejected (see `docs/` research notes): Microsoft Agent
Framework, and the various agent "studios". They model an agent as *their*
object over *their* provider, or require building your orchestration *inside*
them. This project's unit of execution is a **native Claude Code session** — a
shape those frameworks cannot express — and all the data a dashboard or
analytics report needs already sits on disk in a machine-readable form. The
correct investment is a thin observation/control layer over the native model,
not a new orchestration engine underneath it.