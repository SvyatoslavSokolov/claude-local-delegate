# conventions

- The MCP servers speak **hand-rolled stdio JSON-RPC** (line-delimited JSON), not FastMCP.
  `server.py:main()` reads `sys.stdin` line by line, `json.loads`, dispatches via `handle_request`.
  Keep responses on stdout; never print logs to stdout (it would corrupt the protocol) — logs go to stderr.
- Tool surface is defined by data, not code order: the `TOOLS` list and `TOOL_HANDLERS` dict in
  `server.py` are the single source of truth. Tool name strings are also re-exported as
  `mcp__claude-local-delegate__<tool>` references; `SERENA_READ_ONLY_MCP_TOOLS` /
  `DELEGATION_BLOCKED_TOOLS` / `DEFAULT_ALLOWED_TOOLS` gate what a delegate may call.
- Capabilities over defaults: since v0.9 the default worker tool set is a **capability-rich
  writer** (`DEFAULT_ALLOWED_TOOLS` covers real coding), not just `Read,Grep,Glob`.
  Read-only vs mutating is decided by classification, not by the tool name alone.
- Python 3.8 compatibility is a hard constraint (interpreter is 3.8.10). No PEP 604 unions,
  no `match`. Use `Optional` / `Union` and `from __future__ import annotations` where needed.
- The Codex agent directory is a **symlink**, not a copy: `~/.codex/local-delegate-agents` →
  `~/.claude/agents`. Copying it freezes it stale; the installer (`contrib/install_codex.py`)
  recreates the link.
- Do NOT put secrets in `MIGRATION_AND_USAGE_RU.md` or any doc — secrets live in config files
  (e.g. `vllm.settings.json`) that are moved encrypted. Keep paths absolute in docs.
- Use `from __future__ import annotations` is NOT required project-wide; match surrounding style.