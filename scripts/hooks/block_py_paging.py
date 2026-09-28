#!/usr/bin/env python3
"""PreToolUse hook for local delegates: refuse paging a .py file through a pager.

Why a hook and not a sentence in the prompt: on 2026-09-22 a delegate was told
in its task, in capitals, not to page Python files with grep/sed, and spent 200
turns doing exactly that -- the worker persona says Bash searches are allowed,
and the persona won. Two more runs died the same way that week (137 and 221
turns). The looping pattern is always the same: a line window
(`sed -n '2509,2536p' x.py`, `head`/`tail`, `grep -A 40`) re-read again and
again on a 2-3k-line module, never converging.

What is refused, and only on arguments that are .py files:
  * `sed -n` (any line-range print)            * `awk`
  * `head` / `tail`                            * `grep` / `rg` with -A/-B/-C context
What stays allowed on purpose:
  * a plain `grep -n` / `rg -n` that LOCATES an exact string -- this Serena
    setup has no text search, so that is the only way to find a flag, a topic
    name or an error message;
  * `cat`, and the Read tool, reading a whole file once;
  * `head`/`tail`/`grep` on anything that is not a .py file -- above all on
    command output, e.g. `pytest ... | tail -5`.

Set CLAUDE_LOCAL_DELEGATE_ALLOW_PY_PAGING=1 to disable.

Protocol: read the hook JSON on stdin; exit 2 with the reason on stderr to block
(Claude Code shows it to the model), exit 0 to allow.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

_PAGERS = {"sed", "head", "tail", "awk", "gawk"}
_CONTEXT_SEARCH = {"grep", "egrep", "rg"}
_CONTEXT_FLAG = re.compile(r"^-(?:[A-Za-z]*[ABC]\d*|-(?:after|before)?-?context(?:=.*)?)$")

MESSAGE = (
    "Blocked: paging a .py file through {tool} is disabled for delegates -- "
    "re-reading line windows of a large module is the loop that killed runs at "
    "137-326 turns. Instead: mcp__serena__get_symbols_overview on the file, then "
    "mcp__serena__find_symbol with include_body=true for the one symbol you need; "
    "or Read the whole file ONCE. To LOCATE an exact string, a plain "
    "`grep -n '<string>' <file>` (no -A/-B/-C) is still allowed."
)


_OPERATORS = {"|", "||", "&&", ";", "&", "\n"}


def _segments(command: str) -> list[list[str]]:
    """Split a shell command into simple-command token lists on | ; && ||.

    Tokenize first and split on operator TOKENS, never on raw text: an awk or sed
    program in quotes routinely contains `&&` or `|` (`awk 'NR>=1 && NR<=9' x.py`)
    and must stay one argument.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    out, current = [], []
    for token in tokens:
        if token in _OPERATORS or set(token) <= set("|&;"):
            if current:
                out.append(current)
            current = []
        else:
            current.append(token)
    if current:
        out.append(current)
    cleaned = []
    for seg in out:
        while seg and "=" in seg[0] and not seg[0].startswith("-"):
            seg = seg[1:]  # leading VAR=value assignments
        if seg:
            cleaned.append(seg)
    return cleaned


def _is_py(token: str) -> bool:
    return token.endswith(".py") or token.endswith(".py'") or "*.py" in token


def violation(command: str) -> str | None:
    """Return the offending tool name, or None when the command is fine."""
    for tokens in _segments(command):
        tool = os.path.basename(tokens[0])
        if tool in ("sudo", "env", "xargs") and len(tokens) > 1:
            tokens = tokens[1:]
            tool = os.path.basename(tokens[0])
        args = tokens[1:]
        if not any(_is_py(a) for a in args):
            continue
        if tool in _PAGERS:
            if tool == "sed" and "-n" not in args and not any(a.startswith("-n") for a in args):
                # `sed -i 's/a/b/' x.py` edits; it does not page.
                continue
            return tool
        if tool in _CONTEXT_SEARCH and any(_CONTEXT_FLAG.match(a) for a in args):
            return f"{tool} with context lines"
    return None


def main() -> int:
    if os.environ.get("CLAUDE_LOCAL_DELEGATE_ALLOW_PY_PAGING") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # never block on a payload we cannot read
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    tool = violation(command)
    if tool is None:
        return 0
    sys.stderr.write(MESSAGE.format(tool=tool))
    return 2


if __name__ == "__main__":
    sys.exit(main())
