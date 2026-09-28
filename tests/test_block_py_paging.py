"""The .py paging hook: block the loop pattern, keep exact-string search and output tails."""

import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "..", "scripts", "hooks", "block_py_paging.py")
sys.path.insert(0, os.path.dirname(HOOK))
import block_py_paging as hook  # noqa: E402


# Verbatim from the transcripts of the delegates that looped on 2026-09-22.
LOOPED = [
    "sed -n '2509,2536p' external/sim_real_capture/sim_real_capture/segment_report.py",
    "sed -n '910,935p' /x/source/vhal_cli/src/vhal_cli/commands/doctor.py; echo ===",
    "grep -n \"def _stop_in_container\" -A 20 /x/src/vhal_cli/commands/stop.py",
    "grep -n \"def _build_session\" -A 40 /x/tests/test_outputs.py",
    "rg -n \"def _analyze_body_segment\" -A 200 segment_report.py | grep -n motors",
    "head -30 sim_real_capture/excite.py",
    "tail -50 sim_real_capture/excite.py",
    "awk 'NR>=1680 && NR<=1700' sim_real_capture/excite.py",
]

ALLOWED = [
    "grep -n 'LEG_SLOTS' sim_real_capture/excite.py",         # locate an exact string
    "rg -n --include='*.yaml' max_vx profiles/",               # not python
    "python3 -m pytest -q tests/test_pose_r32.py 2>&1 | tail -5",  # tail of output
    "cat sim_real_capture/signals.py",                         # whole-file read
    "sed -i 's/0.3/0.5/' profiles/plan_twist_smoke.yaml",      # yaml edit
    "sed -i 's/old/new/' sim_real_capture/cli.py",             # in-place edit, not paging
    "head -20 output/capture/stdout.log",                      # not python
    "git diff --stat -- sim_real_capture/excite.py",           # no pager tool
    "docker exec c bash -lc 'python3 -m pytest -q' | tail -3", # container tests
]


@pytest.mark.parametrize("command", LOOPED)
def test_the_looping_commands_are_blocked(command):
    assert hook.violation(command) is not None


@pytest.mark.parametrize("command", ALLOWED)
def test_legitimate_commands_pass(command):
    assert hook.violation(command) is None


def _run(payload, env=None):
    return subprocess.run(
        [sys.executable, HOOK], input=json.dumps(payload).encode(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, **(env or {})})


def test_protocol_blocks_with_exit_2_and_explains():
    proc = _run({"tool_name": "Bash", "tool_input": {"command": LOOPED[0]}})
    assert proc.returncode == 2
    msg = proc.stderr.decode()
    assert "find_symbol" in msg and "grep -n" in msg  # says what to do instead


def test_protocol_allows_other_tools_and_clean_commands():
    assert _run({"tool_name": "Read", "tool_input": {"file_path": "x.py"}}).returncode == 0
    assert _run({"tool_name": "Bash", "tool_input": {"command": ALLOWED[0]}}).returncode == 0


def test_opt_out_env_disables_the_block():
    proc = _run({"tool_name": "Bash", "tool_input": {"command": LOOPED[0]}},
                env={"CLAUDE_LOCAL_DELEGATE_ALLOW_PY_PAGING": "1"})
    assert proc.returncode == 0


def test_unreadable_payload_never_blocks():
    proc = subprocess.run([sys.executable, HOOK], input=b"not json",
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.returncode == 0
