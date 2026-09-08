#!/usr/bin/env python3
"""Reviewable, additive Codex registration. No credentials copied."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tomllib


# Codex serializes a tool result into the model's context, so a delegate's long
# final answer is charged to the supervising session. The server already compacts
# these; this is the second, client-side ceiling (docs: tools.<tool>.output_token_limit).
OUTPUT_TOKEN_LIMITS = {
    'get_delegate_result': 12000,
    'get_fanout_result': 16000,
    'get_verified_result': 12000,
    'watch_delegate': 8000,
    'project_sync': 8000,
    'check_delegate_status': 4000,
    'check_fanout_status': 4000,
    'check_verified_status': 4000,
}
TOOL_BUDGETS = ''.join(
    f'\n[mcp_servers.claude-local-delegate.tools.{tool}]\noutput_token_limit = {limit}\n'
    for tool, limit in OUTPUT_TOKEN_LIMITS.items())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--home', type=Path, default=Path.home(), help='Alternate home for testing')
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    repo = Path(__file__).resolve().parent.parent
    config = home / '.codex/config.toml'
    old = config.read_text() if config.exists() else ''
    parsed = tomllib.loads(old)
    name = 'claude-local-delegate'
    existing = parsed.get('mcp_servers', {}).get(name)
    settings = home / '.claude/vllm.delegate.settings.json'
    block = ('\n[mcp_servers.claude-local-delegate]\n'
             'command = "python3"\n'
             f'args = [{json.dumps(str(repo / "server.py"))}]\n'
             'startup_timeout_sec = 20\n'
             'tool_timeout_sec = 240\n'
             '\n[mcp_servers.claude-local-delegate.env]\n'
             f'CLAUDE_LOCAL_DELEGATE_SETTINGS = {json.dumps(str(settings))}\n'
             + TOOL_BUDGETS)
    if existing and (existing.get('command') != 'python3' or existing.get('args') != [str(repo / 'server.py')]):
        raise SystemExit('Existing MCP entry points elsewhere; refusing to overwrite it.')
    changes = {}
    if not existing:
        changes[config] = old.rstrip() + '\n' + block
    marker = '<!-- claude-local-delegate shared supervision -->'
    rules = (f'\n\n{marker}\n'
             '# Shared local delegation and coordination\n\n'
             'For supervising sessions: architecture and final review stay on the main model; '
             'delegate routine work to the local model through claude-local-delegate. '
             'Before project work, read the shared protocol below and call project_sync, '
             'then task_claim. Start only your active reservations; pass task_id to delegates. '
             'Sync at checkpoints and respect other supervisors\' reserved paths.\n\n'
             f'Shared protocol: {repo / "COORDINATION.md"}\n'
             f'Task briefing: {repo / "TASK_DESIGN.md"}\n\n'
             'If running as local-worker or local-checker, execute only the assigned task. '
             'You are the delegated worker, not a supervisor. Never recursively delegate.\n')
    for target in (home / '.codex/AGENTS.md', home / '.claude/CLAUDE.md'):
        text = target.read_text() if target.exists() else ''
        if marker not in text:
            changes[target] = text.rstrip() + rules
    link = home / '.codex/local-delegate-agents'
    agents = home / '.claude/agents'
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != agents.resolve():
            raise SystemExit(f'Refusing to replace existing path: {link}')
    print(block)
    print('Files:', *(str(p) for p in changes), sep='\n  ')
    print('Persona link:', link, '->', agents)
    if not args.apply:
        print('Dry run. Pass --apply to install.')
        return
    if not settings.is_file() or not agents.is_dir():
        raise SystemExit('Shared Claude settings/persona directory is missing.')
    # Validate complete resulting TOML before touching any file.
    tomllib.loads(changes.get(config, old))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    for target, text in changes.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            backup = target.with_name(target.name + '.before-local-delegate-' + stamp)
            shutil.copy2(target, backup)
            print('Backup:', backup)
        tmp = target.with_name(target.name + '.local-delegate-' + stamp + '.tmp')
        tmp.write_text(text)
        tmp.chmod(target.stat().st_mode & 0o777 if target.exists() else 0o600)
        os.replace(tmp, target)
    if not link.is_symlink():
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(agents, target_is_directory=True)
    print('Installed. Restart Codex and reconnect/restart the Claude MCP.')


if __name__ == '__main__':
    main()
