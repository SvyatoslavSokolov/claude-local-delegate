#!/usr/bin/env python3
"""Reviewable, additive Codex registration. No credentials copied."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tomllib


# Codex serializes a tool result into the model's context, so a delegate's long
# final answer is charged to the supervising session. The server already compacts
# these; this is the second, client-side ceiling (docs: tools.<tool>.output_token_limit).
OUTPUT_TOKEN_LIMITS = {
    'get_delegate_result': 4000,
    'get_fanout_result': 8000,
    'get_verified_result': 6000,
    'watch_delegate': 4000,
    'project_sync': 4000,
    'check_delegate_status': 2000,
    'check_fanout_status': 2000,
    'check_verified_status': 2000,
}
TOOL_BUDGETS = ''.join(
    f'\n[mcp_servers.claude-local-delegate.tools.{tool}]\noutput_token_limit = {limit}\n'
    for tool, limit in OUTPUT_TOKEN_LIMITS.items())


def upsert_table_value(text, table, key, value):
    """Set one scalar without rewriting unrelated TOML or table-local options."""
    header = '[' + table + ']'
    match = re.search(r'(?m)^' + re.escape(header) + r'[ \t]*$', text)
    assignment = f'{key} = {value}'
    if not match:
        return text.rstrip() + f'\n\n{header}\n{assignment}\n'
    following = text[match.end():]
    next_header = re.search(r'(?m)^\[', following)
    end = len(text) if not next_header else match.end() + next_header.start()
    body = text[match.end():end]
    current = re.search(r'(?m)^[ \t]*' + re.escape(key) + r'[ \t]*=.*$', body)
    if current:
        body = body[:current.start()] + assignment + body[current.end():]
    else:
        body = body.rstrip() + '\n' + assignment + '\n'
    return text[:match.end()] + body + text[end:]


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
        updated_config = old.rstrip() + '\n' + block
    else:
        updated_config = old
        for tool, limit in OUTPUT_TOKEN_LIMITS.items():
            updated_config = upsert_table_value(
                updated_config,
                f'mcp_servers.claude-local-delegate.tools.{tool}',
                'output_token_limit', limit)
    if updated_config != old:
        changes[config] = updated_config
    marker = '<!-- claude-local-delegate shared supervision -->'
    end_marker = '<!-- /claude-local-delegate shared supervision -->'
    rules = (f'{marker}\n'
             '# Shared local delegation and coordination\n\n'
             'For supervising sessions, read the compact brief, then call project_sync and '
             'task_claim. Keep architecture and final review on the main model. Delegate only '
             'focused mechanical work; normally wait with '
             'get_delegate_result(wait_seconds=120) instead of polling.\n\n'
             f'Compact brief: {repo / "ARCHITECT_BRIEF.md"}\n'
             f'Conflict/recovery reference: {repo / "COORDINATION.md"}\n'
             f'Detailed task-design reference: {repo / "TASK_DESIGN.md"}\n\n'
             'If running as local-worker or local-checker, execute only the assigned task. '
             'You are the delegated worker, not a supervisor. Never recursively delegate.\n'
             f'{end_marker}\n')
    for target in (home / '.codex/AGENTS.md', home / '.claude/CLAUDE.md'):
        text = target.read_text() if target.exists() else ''
        if marker in text:
            start = text.index(marker)
            end = text.find(end_marker, start)
            # Older installer versions always appended their unterminated managed
            # block at EOF, so replacing that suffix is safe and makes future
            # updates exactly bounded by begin/end markers.
            end = len(text) if end < 0 else end + len(end_marker)
            updated = text[:start].rstrip() + '\n\n' + rules + text[end:].lstrip('\n')
        else:
            updated = text.rstrip() + '\n\n' + rules
        if updated != text:
            changes[target] = updated
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
