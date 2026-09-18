#!/usr/bin/env python3
"""Standalone health check for the local vLLM delegation backend.

Checks:
  1. ANTHROPIC_BASE_URL from ~/.claude/vllm.delegate.settings.json (reachable? 5s timeout)
  2. coordination.sqlite3 task counts (active vs done)

Exit code: 0 if the backend is reachable, 1 otherwise.
Usage: health_check.py [--json]
"""

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

SETTINGS_PATH = os.path.expanduser("~/.claude/vllm.delegate.settings.json")
DB_PATH = os.path.expanduser("~/.claude-local-delegate/coordination.sqlite3")
PROBE_TIMEOUT = 5.0


def read_base_url():
    try:
        with open(SETTINGS_PATH) as f:
            settings = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"cannot read {SETTINGS_PATH}: {e}"
    url = (settings.get("env") or {}).get("ANTHROPIC_BASE_URL")
    if not url:
        return None, f"ANTHROPIC_BASE_URL not set in {SETTINGS_PATH}"
    return url, None


def probe_base_url(base_url):
    """HTTP ping of the backend root. Returns (reachable, detail)."""
    probe = base_url.rstrip("/") + "/"
    try:
        with urllib.request.urlopen(probe, timeout=PROBE_TIMEOUT) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        # Server answered with an HTTP error status: it is reachable.
        return True, f"HTTP {e.code} (server up)"
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return False, f"unreachable: {e}"


def count_tasks():
    """Count task statuses from coordination.sqlite3. Returns dict or None + error."""
    if not os.path.exists(DB_PATH):
        return None, f"database not found: {DB_PATH}"
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        rows = con.execute("SELECT body FROM tasks").fetchall()
        con.close()
    except sqlite3.Error as e:
        return None, f"database error: {e}"
    counts = {}
    for (body,) in rows:
        try:
            status = json.loads(body).get("status", "unknown")
        except (ValueError, AttributeError):
            status = "unknown"
        counts[status] = counts.get(status, 0) + 1
    done = counts.get("done", 0) + counts.get("cancelled", 0)
    total = sum(counts.values())
    return {"total": total, "active": total - done, "done": done, "by_status": counts}, None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = parser.parse_args()

    base_url, err = read_base_url()
    if base_url:
        reachable, detail = probe_base_url(base_url)
    else:
        reachable, detail = False, err

    tasks, task_err = count_tasks()

    result = {
        "settings": SETTINGS_PATH,
        "base_url": base_url,
        "backend_reachable": reachable,
        "backend_detail": detail,
        "tasks": tasks,
        "tasks_error": task_err,
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("Local vLLM delegation backend health check")
        print(f"  backend : {base_url or 'n/a'}")
        status = "OK" if reachable else "UNREACHABLE"
        print(f"  probe   : {status} ({detail})")
        if tasks:
            print(f"  tasks   : {tasks['active']} active / {tasks['done']} done "
                  f"(total {tasks['total']}) {tasks['by_status']}")
        else:
            print(f"  tasks   : unavailable ({task_err})")

    sys.exit(0 if reachable else 1)


if __name__ == "__main__":
    main()