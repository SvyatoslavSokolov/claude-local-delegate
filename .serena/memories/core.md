# core

Purpose: MCP server that lets a Claude Code (or Codex) main session delegate coding
tasks to a local model (vLLM) while staying on the main subscription. Every delegation
spawns a **native `claude --bg` agent** pointed at the local backend via `--settings`
(see `mem:conventions` for how the server stays stdio-only).

Entrypoints (all `python3 <file>` from repo root, registered as MCP in `~/.claude.json`):
- `server.py` — the delegation MCP. `main()` (server.py:2277) is a hand-rolled
  stdio JSON-RPC loop (NOT FastMCP). Tool dispatch via the `TOOL_HANDLERS` dict + `TOOLS` list.
- `code_nav_server.py` — a separate tiny MCP (`code_nav_server.py:1`) exposing ONE tool,
  `repository_route`, which only selects child maps/paths from `docs/design/repository_map.yaml`.
  It does no search/index itself.

Separation of concerns (important invariant):
- **Serena** owns semantic code navigation (symbol overview / declaration / references /
  diagnostics / bounded pattern search / read-only memory reads). Read-only delegates are
  granted exactly `server.SERENA_READ_ONLY_MCP_TOOLS`; onboarding + all mutating tools excluded.
- Delegates are denied the recursive-delegation MCP, but keep the map router + Serena read-only tools. Bash stays available.
- Keeping `code_nav_server.py` (map router) and `server.py` (delegation) as separate processes
  is deliberate — it is what lets you grant read-only nav without granting re-delegation.

Reference other memories:
- Tech, deps, versions, launch commands: `mem:tech_stack`
- Commands the user runs (dev/test/run/entrypoints): `mem:suggested_commands`
- Style + design patterns specific to this codebase: `mem:conventions`
- What "done" means (test/lint/typecheck): `mem:task_completion`