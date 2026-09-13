# task_completion

Tests are `unittest`-based (no pytest, no pytest.ini/tox). Each top-level `test_*.py` in the
repo root is a standalone runner with `if __name__ == "__main__": unittest.main()` at the bottom
(e.g. `test_runtime.py:157`, `test_metrics.py:284`). Run a single file directly:

- `python3 test_runtime.py`
- `python3 test_metrics.py`
- `python3 test_coordination.py`, `python3 test_backend.py`, `python3 test_concurrency.py`,
  `python3 test_efficiency.py`, `python3 test_delegate_report.py`

`tests/` holds extra unit tests (`test_code_nav.py`, `test_code_nav_server.py`,
`test_local_worker_search_policy.py`, `test_tool_capabilities.py`,
`test_history_stats_search_churn.py`) — run them the same way (`python3 tests/<file>.py`).
Integration tests avoid model calls and user-state writes (use a temp
`CLAUDE_LOCAL_DELEGATE_STATE_DIR`).

There is no linter/formatter/type-checker configured in-repo. A task is "done" when the
relevant `test_*.py` file(s) pass. No build or packaging step exists to run.