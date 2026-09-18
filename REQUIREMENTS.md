# claude-local-delegate — requirements

The contract this repository is built to satisfy: what it needs to run, the
structural rules that keep it a clean self-contained tool, and the quality
bar for the analytics it produces. `README.md` is the overview; this file is
the "why it is shaped this way" spec.

## 1. Purpose (one line)

A set of MCP servers that let a Claude Code (or Codex) supervisor hand a
self-contained task to a **native `claude --bg` background agent running on a
local model** (e.g. vLLM), and inspect / coordinate / score the result — while
the supervising session stays on its regular (paid) model.

## 2. Runtime requirements (to run it on a machine)

| Requirement | Detail |
|---|---|
| `claude` CLI on `PATH` | Claude Code. The spawn is a real `claude --bg` process; there is no bundled runtime. |
| Python 3 (≥3.8), **stdlib only** | No `pip install`. Every module and every `contrib/` CLI runs on the standard library alone. |
| Local model gateway | An Anthropic-compatible HTTP(S) gateway (e.g. vLLM + LiteLLM) reachable from the box. |
| Settings profile | A `--settings` JSON pointing Claude Code at that gateway. Default `~/.claude/vllm.delegate.settings.json` (fallback `~/.claude/vllm.settings.json`), override with `CLAUDE_LOCAL_DELEGATE_SETTINGS`. **Contains secrets — never commit it.** |
| Personas (optional but default) | `~/.claude/agents/local-worker.md` (writer default) and `~/.claude/agents/local-checker.md` (verified-loop checker). Absence degrades gracefully to no persona. |
| Two MCP registrations (user scope) | `claude-local-delegate` → `server.py`, and `code-nav` → `code_nav_server.py`. See `README.md` / `MIGRATION_AND_USAGE_RU.md`. |
| `worktree.bgIsolation: "none"` | Required for delegated writers to `Write`/`Edit` in the main checkout. |

State is written under `~/.claude-local-delegate/` (override
`CLAUDE_LOCAL_DELEGATE_STATE_DIR`): `coordination.sqlite3` (task board + events),
`metrics.jsonl` (spawn/rate/blocked ledger), `runs.json`, `verified/*.json`,
`batches/*.json`, `session-starts.json`, `inflight.json`.

## 3. Repository-structure requirements (the architecture contract)

The layout is deliberately a **flat runtime core + organized support dirs**,
**not** a package. These constraints are load-bearing — changing any of them
silently breaks an external dependency:

1. **`server.py` and `code_nav_server.py` stay at the repo root.** The MCP
   registrations in `~/.claude.json` invoke them by **absolute path**. Moving
   either forces an external-config edit; the flat root is the cheapest correct
   shape.
2. **The operational docs stay at the root** (`ARCHITECT_BRIEF.md`,
   `COORDINATION.md`, `TASK_DESIGN.md`, `SUPERVISION.md`,
   `MIGRATION_AND_USAGE_RU.md`). The supervisor's global `CLAUDE.md` references
   the first three by **absolute path**; relocating them breaks the low-context
   supervisor path.
3. **The core stays a flat set of importable modules** (`server.py`,
   `coordination.py`, `coordination_runtime.py`, `local_backend.py`,
   `code_nav.py`, `metrics.py`) with **bare imports** (`import metrics`,
   `from coordination import Board`). Converting to a `package/` would break
   both the bare imports and the path-based MCP entry points for no benefit to a
   single-user tool.
4. **`contrib/` CLIs are self-contained.** Each (`history_stats.py`,
   `delegate_report.py`, `report.py`, …) self-locates the repo root on
   `sys.path` and imports only stdlib (plus `metrics` where it needs the shared
   readers). They must run standalone as `python3 contrib/<name>.py` — do not
   couple them to the server process.
5. **All tests live in `tests/`** and must run **both** ways:
   `python3 -m pytest` **and** `python3 tests/<file>.py` standalone. Every test
   self-locates the repo root (see `tests/conftest.py` for the pytest path and
   the per-file `sys.path.insert` for direct runs).
6. **Planning/review notes live in `docs/`**; generated artifacts and caches
   (`analysis/`, `__pycache__/`, `.pytest_cache/`) are git-ignored, not tracked.

## 4. Analytics requirements (the quality loop)

`contrib/report.py` closes the "analytics → improve the pipeline" loop. It must:

- **Read only**, via the shared `metrics.py` readers (`load_ledger`,
  `load_verified`, `load_batches`, `percentile`, `parse_timestamp`) — never
  re-parse the state dir, and never require the server to be running.
- **Never raise** on missing data (absent state dir, empty ledger, missing
  sqlite, empty `verified/`/`batches/` → "no data" lines, exit 0).
- **Emit a flat per-task CSV** (one row per delegation, plain columns, no nested
  JSON) that `pandas.read_csv` loads directly. `quality` and `worth_it`
  (0–100) are first-class columns — they are the core of the quality model and
  must be preserved. Columns: `run_id, task_id, task_key, name, project, cwd,
  created_at, finished_at, duration_s, complexity, est_minutes, profile, model,
  read_only, prompt_chars, quality, worth_it, api_calls, output_tokens,
  input_tokens, cached_input_tokens, peak_context, thinking_chars, text_chars,
  tool_calls, blocked_category, n_blocked, verified, verified_iterations,
  batch_id, task_status`.
- **Surface a trend** (worth_it / quality over time, ISO weeks) and a
  **health-index** across the pipeline areas that matter: blocked-by-category,
  verified revise-rate, fan-out batch fail-rate, delta-vs-baseline, and
  complexity×profile cells.
- **Emit insights** — a sorted "problem → named action" list driven by
  **module-level threshold constants** (tunable in one place, not scattered).
- Be **machine-readable** (`--json`) for scripting.

The `blocked` signal must be **persisted** (one `blocked` event per blocked
episode in `metrics.jsonl`, written by `server.py` on the state transition, not
per poll) so the report can aggregate it historically.

## 5. Quality / governance requirements

- **Tests are the gate.** Any change must keep `python3 -m pytest` green and
  every `tests/*.py` green standalone. Do not skip or delete tests to pass.
- **Never break a spawn.** The metrics-ledger append and all analytics are
  best-effort: they swallow I/O and serialization errors so a stats failure can
  never stop a delegation.
- **Least privilege by default.** Delegated writers default to a capable tool
  set but the subagent-spawning tools (`Agent`/`Task`) are never granted and the
  recursive delegation MCP is always disallowed, so a delegated agent cannot grow
  unbounded delegation depth.
- **No AI attribution in commits.** Commits carry no `Co-Authored-By` / AI
  lines (see the user's global rules).
- **Stdlib only.** Introducing a third-party dependency requires a deliberate,
  documented decision — the "no pip" guarantee is a feature, not an accident.