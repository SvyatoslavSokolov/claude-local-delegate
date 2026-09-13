# tech_stack

- Language: Python. Local interpreter is **3.8.10** — keep 3.8-compatible syntax
  (no `match`, no `X | Y` unions in annotations at runtime, no `list[str]` without `from __future__`).
- No `pyproject.toml` / `requirements.txt` / `setup.py` — the repo is not packaged.
  Modules are imported directly (`python3 server.py`). Third-party deps are minimal and
  stdio-JSON-RPC based, not FastMCP.
- State lives in a per-user dir (default `~/.claude-local-delegate/`); override with the
  `CLAUDE_LOCAL_DELEGATE_STATE_DIR` env var (tests use this to isolate). The coordination
  board is a local SQLite (`coordination.sqlite3`) shared by all client processes under one OS user.
- Two separately-registered MCP servers: the delegation server (`server.py`) and the
  repository-map router (`code_nav_server.py`). Serena is a third, user-scoped MCP providing
  semantic navigation + read-only tool set to delegates.
- Local model backend is served by vLLM (see `vllm.settings.json` / `local_backend.py`).
  `local_backend.py` exposes a small `profile/environment/inspect` surface used to verify the route.
- Codex integration: `contrib/install_codex.py`; the Codex agent dir is a **symlink**
  `~/.codex/local-delegate-agents` → `~/.claude/agents` (recreate the link, never copy it — see
  `mem:conventions`).
- Stats/analysis helpers (no model calls): `contrib/history_stats.py`, `analysis/analyze.py`.