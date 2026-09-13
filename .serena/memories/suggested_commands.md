# suggested_commands

Run from the repo root. There is no build step and no packaging.

- Run the delegation MCP server (for local debugging over stdio): `python3 server.py`
- Run the map-router MCP: `python3 code_nav_server.py`
- Register as user-scope MCP: entries live in `~/.claude.json` → `mcpServers`
  (NOT `settings.json`). Each command is `python3 <abs>/server.py` etc. (see MIGRATION doc, part A).
- Stats over local session history (prints aggregates only — no prompts/answers/credentials):
  `python3 contrib/history_stats.py --pretty`
- Codex install (recreates the agent symlink, does not copy it): `python3 contrib/install_codex.py`
- Concurrency benchmark: `python3 contrib/bench_concurrency.py`
- Serena: run `serena` / `serena start-mcp-server --context=claude-code --project-from-cwd`
  from the project dir; project config is `.serena/project.yml`, per-user overrides in `.serena/project.local.yml`.

Note on test invocation: tests are `unittest`-based (no pytest dependency), see `mem:task_completion`.