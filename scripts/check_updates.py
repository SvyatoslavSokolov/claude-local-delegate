#!/usr/bin/env python3
"""Check installed versions and available updates for the agent ecosystem.

Checks:
1. Claude Code CLI (@anthropic-ai/claude-code via npm registry).
2. OpenResearch CLI (orx version --check via GitHub releases).
3. Google Antigravity CLI (agy).
4. Python core test dependencies (pytest via PyPI API).
5. Remote GPU cluster gateway (vLLM / LiteLLM health).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from typing import Any, Dict, List, Optional


def check_claude() -> Dict[str, Any]:
    """Check Claude Code installed vs npm registry version."""
    curr = None
    try:
        proc = subprocess.run(["claude", "--version"], stdout=subprocess.PIPE, text=True, timeout=5)
        if proc.returncode == 0:
            match = re.search(r"(\d+\.\d+\.\d+)", proc.stdout)
            if match:
                curr = match.group(1)
    except Exception as e:
        curr = None

    latest = None
    try:
        proc = subprocess.run(
            ["npm", "view", "@anthropic-ai/claude-code", "version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            latest = proc.stdout.strip()
    except Exception:
        pass

    has_update = bool(curr and latest and curr != latest)
    return {
        "name": "Claude Code CLI",
        "installed": curr or "not installed",
        "latest": latest or "unknown",
        "update_available": has_update,
        "update_cmd": "npm install -g @anthropic-ai/claude-code" if has_update else None,
    }


def check_orx() -> Dict[str, Any]:
    """Check OpenResearch (orx) installed vs remote release."""
    orx_bin = (
        os.environ.get("ORX_BIN")
        or shutil.which("orx")
        or (os.path.expanduser("~/.cargo/bin/orx") if os.path.isfile(os.path.expanduser("~/.cargo/bin/orx")) else None)
    )
    if not orx_bin:
        return {
            "name": "OpenResearch CLI (orx)",
            "installed": "not installed",
            "latest": "unknown",
            "update_available": False,
            "update_cmd": "curl -LsSf https://openresearch.sh/install.sh | sh",
        }

    curr = "0.2.4"
    is_up_to_date = True
    try:
        proc = subprocess.run(
            [orx_bin, "version", "--check"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
        )
        out = proc.stdout.strip()
        m = re.search(r"orx\s+([\d\.]+)", out)
        if m:
            curr = m.group(1)
        is_up_to_date = "is up to date" in out
    except Exception:
        pass

    return {
        "name": "OpenResearch CLI (orx)",
        "installed": curr,
        "latest": curr if is_up_to_date else "newer available",
        "update_available": not is_up_to_date,
        "update_cmd": "orx update" if not is_up_to_date else None,
    }


def check_agy() -> Dict[str, Any]:
    """Check Google Antigravity CLI version."""
    agy_bin = shutil.which("agy") or os.path.expanduser("~/.local/bin/agy")
    curr = None
    if os.path.isfile(agy_bin) and os.access(agy_bin, os.X_OK):
        try:
            proc = subprocess.run([agy_bin, "--version"], stdout=subprocess.PIPE, text=True, timeout=5)
            if proc.returncode == 0:
                curr = proc.stdout.strip()
        except Exception:
            pass

    return {
        "name": "Antigravity CLI (agy)",
        "installed": curr or "not installed",
        "latest": "managed via internal updater",
        "update_available": False,
        "update_cmd": None,
    }


def check_pytest() -> Dict[str, Any]:
    """Check pytest installed vs PyPI."""
    curr = None
    try:
        import pytest
        curr = pytest.__version__
    except ImportError:
        pass

    latest = None
    try:
        req = urllib.request.Request(
            "https://pypi.org/pypi/pytest/json",
            headers={"User-Agent": "claude-local-delegate"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.load(resp)
            latest = data.get("info", {}).get("version")
    except Exception:
        pass

    has_update = bool(curr and latest and curr != latest)
    return {
        "name": "Python pytest",
        "installed": curr or "not installed",
        "latest": latest or "unknown",
        "update_available": has_update,
        "update_cmd": f"pip install --upgrade pytest" if has_update else None,
    }


def check_cluster(base_url: str = "http://192.168.1.109:4000") -> Dict[str, Any]:
    """Check reachability of the remote vLLM / LiteLLM cluster gateway."""
    status = "unreachable"
    detail = None
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/health/readiness")
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status == 200:
                status = "healthy"
                detail = "HTTP 200 OK"
    except Exception as e:
        try:
            # Fallback probe to root
            with urllib.request.urlopen(base_url.rstrip("/"), timeout=3) as resp:
                status = "healthy"
                detail = f"HTTP {resp.status}"
        except Exception as e2:
            detail = str(e2)

    return {
        "name": "vLLM / LiteLLM Cluster",
        "installed": base_url,
        "latest": status,
        "update_available": False,
        "update_cmd": None,
        "detail": detail,
    }


def collect_all_updates() -> List[Dict[str, Any]]:
    """Gather version and update data across all ecosystem components."""
    return [
        check_claude(),
        check_orx(),
        check_agy(),
        check_pytest(),
        check_cluster(),
    ]


def format_table(results: List[Dict[str, Any]]) -> str:
    """Format update results as a clean terminal table."""
    lines = []
    lines.append("=" * 80)
    lines.append("  ECOSYSTEM DEPENDENCIES & UPDATES MONITOR")
    lines.append("=" * 80)
    lines.append(f"{'Component':<28} {'Installed':<16} {'Status / Latest':<20} {'Action'}")
    lines.append("-" * 80)

    for item in results:
        name = item["name"]
        installed = str(item.get("installed", "-"))
        latest = str(item.get("latest", "-"))
        update_avail = item.get("update_available")
        cmd = item.get("update_cmd")

        if update_avail:
            badge = f"⚡ UPDATE -> {latest}"
            action = cmd or "upgrade"
        elif item.get("latest") == "unreachable":
            badge = "❌ UNREACHABLE"
            action = item.get("detail", "")
        else:
            badge = "✅ UP TO DATE"
            action = "-"

        lines.append(f"{name:<28} {installed:<16} {badge:<20} {action}")

    lines.append("-" * 80)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Check ecosystem dependencies and available updates.")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format.")
    args = parser.parse_args()

    results = collect_all_updates()
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(format_table(results))


if __name__ == "__main__":
    main()
