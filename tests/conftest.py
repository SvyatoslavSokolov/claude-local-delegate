# conftest.py: make the repo root importable for tests moved from root.
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Surface ecosystem update notices at the end of test runs for LLMs and developers."""
    try:
        from scripts.check_updates import load_cache
        cached = load_cache(max_age_seconds=86400)
        if not cached:
            return
        updates = [r for r in cached if r.get("update_available")]
        if updates:
            terminalreporter.section("Ecosystem Updates Notice", yellow=True, bold=True)
            for u in updates:
                terminalreporter.write_line(
                    f"  ⚡ {u['name']}: {u.get('installed')} -> {u.get('latest')} ({u.get('update_cmd')})"
                )
            terminalreporter.write_line("  💡 Run: python3 scripts/check_updates.py --auto-update\n")
    except Exception:
        pass