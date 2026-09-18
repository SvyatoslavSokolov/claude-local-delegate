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
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

CACHE_FILE = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_UPDATES_CACHE",
    os.path.expanduser("~/.claude-local-delegate/ecosystem-updates.json"),
)
CACHE_TTL_SECONDS = 14400  # 4 hours


def check_claude() -> Dict[str, Any]:
    """Check Claude Code installed vs npm registry version."""
    curr = None
    try:
        proc = subprocess.run(["claude", "--version"], stdout=subprocess.PIPE, text=True, timeout=5)
        if proc.returncode == 0:
            match = re.search(r"(\d+\.\d+\.\d+)", proc.stdout)
            if match:
                curr = match.group(1)
    except Exception:
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
    update_cmd = "npm install -g @anthropic-ai/claude-code"
    # If user uses a custom npm prefix (like ~/.npm-global), honor it
    npm_global = os.path.expanduser("~/.npm-global/bin/claude")
    if os.path.isfile(npm_global):
        update_cmd = "npm install -g --prefix ~/.npm-global @anthropic-ai/claude-code"

    return {
        "name": "Claude Code CLI",
        "installed": curr or "not installed",
        "latest": latest or "unknown",
        "update_available": has_update,
        "update_cmd": update_cmd if has_update else None,
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
    """Check pytest installed vs PyPI and local Python compatibility."""
    curr = None
    try:
        import pytest
        curr = pytest.__version__
    except ImportError:
        pass

    latest = None
    requires_py = None
    try:
        req = urllib.request.Request(
            "https://pypi.org/pypi/pytest/json",
            headers={"User-Agent": "claude-local-delegate"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.load(resp)
            info = data.get("info", {})
            latest = info.get("version")
            requires_py = info.get("requires_python")
    except Exception:
        pass

    has_update = bool(curr and latest and curr != latest)

    # Check whether the latest version on PyPI is installable on this Python interpreter
    if has_update and requires_py:
        try:
            from packaging.specifiers import SpecifierSet
            from packaging.version import Version
            import platform
            if Version(platform.python_version()) not in SpecifierSet(requires_py):
                # Latest release on PyPI cannot run on this Python. Check if pip has an upgrade:
                proc = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "--dry-run", "--upgrade", "pytest"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=5,
                )
                m = re.search(r"Would install\s+pytest-([\d\.\w\-]+)", proc.stdout)
                if m:
                    latest = m.group(1)
                    has_update = True
                else:
                    latest = curr
                    has_update = False
        except Exception:
            pass

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
    except Exception:
        try:
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


def load_cache(cache_file: str = CACHE_FILE, max_age_seconds: int = CACHE_TTL_SECONDS) -> Optional[List[Dict[str, Any]]]:
    """Load cached update results if within TTL."""
    try:
        p = Path(cache_file)
        if not p.is_file():
            return None
        mtime = p.stat().st_mtime
        if (time.time() - mtime) > max_age_seconds:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return None


def save_cache(results: List[Dict[str, Any]], cache_file: str = CACHE_FILE) -> None:
    """Save update results to cache file atomically."""
    try:
        p = Path(cache_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": time.time(),
            "results": results,
        }
        tmp_p = p.with_suffix(".tmp")
        tmp_p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_p.replace(p)
    except Exception:
        pass


def apply_updates(results: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Execute update commands for any components that have available updates."""
    if results is None:
        results = collect_all_updates()

    applied = []
    for item in results:
        if item.get("update_available") and item.get("update_cmd"):
            cmd = item["update_cmd"]
            name = item["name"]
            try:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=120,
                )
                applied.append({
                    "name": name,
                    "command": cmd,
                    "returncode": proc.returncode,
                    "success": proc.returncode == 0,
                    "stdout": proc.stdout.strip()[:500],
                    "stderr": proc.stderr.strip()[:500],
                })
            except Exception as exc:
                applied.append({
                    "name": name,
                    "command": cmd,
                    "returncode": -1,
                    "success": False,
                    "error": str(exc),
                })

    return applied


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


def format_banner(results: List[Dict[str, Any]]) -> str:
    """Format a concise banner suitable for sys.stderr during MCP initialization."""
    updates = [r for r in results if r.get("update_available")]
    if updates:
        lines = ["[claude-local-delegate] ⚡ Ecosystem updates available:"]
        for u in updates:
            lines.append(
                f"[claude-local-delegate]   • {u['name']}: {u.get('installed')} -> {u.get('latest')} (cmd: {u.get('update_cmd')})"
            )
        lines.append("[claude-local-delegate]   To update automatically: python3 scripts/check_updates.py --auto-update")
        return "\n".join(lines)

    unreachable = [r for r in results if r.get("latest") == "unreachable"]
    if unreachable:
        return f"[claude-local-delegate] ⚠️ Warning: {unreachable[0]['name']} unreachable ({unreachable[0].get('detail', '')})"

    summary = []
    for r in results:
        if r.get("name") == "Claude Code CLI":
            summary.append(f"Claude {r.get('installed')}")
        elif r.get("name") == "OpenResearch CLI (orx)":
            summary.append(f"orx {r.get('installed')}")
        elif r.get("name") == "Python pytest":
            summary.append(f"pytest {r.get('installed')}")
        elif r.get("name") == "vLLM / LiteLLM Cluster":
            summary.append("cluster healthy")

    return f"[claude-local-delegate] 🟢 Ecosystem up to date ({', '.join(summary)})"


def notify_on_startup(background: bool = True, force: bool = False, auto_update: bool = False) -> None:
    """Check ecosystem and output status to sys.stderr on MCP initialization.

    Adheres strictly to MCP stdio requirements:
    - Never prints to sys.stdout (preserving JSON-RPC framing).
    - Uses fresh cache when available for immediate 0ms response.
    - If cache is stale or missing, queries in a background thread to prevent handshake delays.
    - If auto_update is True or CLAUDE_LOCAL_DELEGATE_AUTO_UPDATE=1 is set, applies updates in background.
    """
    def _run():
        cached = None if force else load_cache()
        if cached is None:
            results = collect_all_updates()
            save_cache(results)
        else:
            results = cached

        should_auto = auto_update or os.environ.get("CLAUDE_LOCAL_DELEGATE_AUTO_UPDATE", "").lower() in ("1", "true", "yes")
        if should_auto and any(r.get("update_available") for r in results):
            sys.stderr.write("[claude-local-delegate] 🔄 Auto-updating ecosystem dependencies...\n")
            sys.stderr.flush()
            apply_updates(results)
            results = collect_all_updates()
            save_cache(results)

        banner = format_banner(results)
        sys.stderr.write(banner + "\n")
        sys.stderr.flush()

    # Fast path: if cache exists and fresh, output immediately
    cached_data = load_cache() if not force else None
    if cached_data is not None and not auto_update and os.environ.get("CLAUDE_LOCAL_DELEGATE_AUTO_UPDATE", "").lower() not in ("1", "true", "yes"):
        banner = format_banner(cached_data)
        sys.stderr.write(banner + "\n")
        sys.stderr.flush()
        return

    if background:
        import threading
        thread = threading.Thread(target=_run, daemon=True, name="ecosystem-update-checker")
        thread.start()
    else:
        _run()


def main():
    parser = argparse.ArgumentParser(description="Check ecosystem dependencies and available updates.")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format.")
    parser.add_argument("--banner", action="store_true", help="Output compact MCP stderr banner.")
    parser.add_argument("--force", action="store_true", help="Ignore cache and force live check.")
    parser.add_argument("--auto-update", "--upgrade", action="store_true", help="Automatically install available updates.")
    args = parser.parse_args()

    if args.auto_update:
        print("[*] Checking for ecosystem updates...")
        results = collect_all_updates()
        updates_needed = [r for r in results if r.get("update_available")]
        if not updates_needed:
            print("All ecosystem components are already up to date!")
            save_cache(results)
            return

        print(f"[*] Found {len(updates_needed)} update(s). Applying now...")
        applied = apply_updates(results)
        for act in applied:
            status = "✅ SUCCESS" if act["success"] else "❌ FAILED"
            print(f"  [{status}] {act['name']} ({act['command']})")
            if act.get("output"):
                print(f"      {act['output']}")

        print("[*] Re-verifying versions after update...")
        fresh = collect_all_updates()
        save_cache(fresh)
        print(format_table(fresh))
        return

    cached = None if args.force else load_cache()
    if cached is not None:
        results = cached
    else:
        results = collect_all_updates()
        save_cache(results)

    if args.json:
        print(json.dumps(results, indent=2))
    elif args.banner:
        print(format_banner(results))
    else:
        print(format_table(results))


if __name__ == "__main__":
    main()
