#!/usr/bin/env python3
"""
tree_monitor.py - Hierarchical Tree View Monitor for Multi-Agent Orchestration.

Unifies:
- Native Claude Code background agents (claude agents --json)
- Coordination tasks from coordination.sqlite3
- Local vLLM workers (runs.json on 4x 3090)
- Google Antigravity / Gemini tasks (agy_runs)

Provides a single hierarchical point of entry to inspect all running & recent agents.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

STATE_DIR = Path(os.environ.get("CLAUDE_LOCAL_DELEGATE_STATE_DIR", "~/.claude-local-delegate")).expanduser()
DB_PATH = STATE_DIR / "coordination.sqlite3"
RUNS_PATH = STATE_DIR / "runs.json"
AGY_RUNS_DIR = STATE_DIR / "agy_runs"


def load_runs() -> Dict[str, Any]:
    if RUNS_PATH.exists():
        try:
            data = json.loads(RUNS_PATH.read_text(encoding="utf-8"))
            runs = data.get("runs")
            return runs if isinstance(runs, dict) else {}
        except Exception:
            return {}
    return {}


def load_tasks() -> List[Dict[str, Any]]:
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute("SELECT id, project, task_key, owner, body FROM tasks ORDER BY ROWID DESC LIMIT 50;")
        tasks = []
        for row in cur.fetchall():
            try:
                body = json.loads(row[4])
                body["db_task_key"] = row[2]
                body["db_owner"] = row[3]
                tasks.append(body)
            except Exception:
                continue
        conn.close()
        return tasks
    except Exception:
        return []


def load_agy_runs() -> List[Dict[str, Any]]:
    runs = []
    if AGY_RUNS_DIR.exists():
        for p in sorted(AGY_RUNS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[:30]:
            try:
                runs.append(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                continue
    return runs


def load_claude_agents() -> List[Dict[str, Any]]:
    try:
        proc = subprocess.run(
            ["claude", "agents", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return json.loads(proc.stdout)
    except Exception:
        pass
    return []


def build_hierarchy() -> Dict[str, Any]:
    tasks = load_tasks()
    runs = load_runs()
    agy_runs = load_agy_runs()
    claude_agents = load_claude_agents()

    # Map of claude agent short_id -> live status
    live_agents = {}
    for ca in claude_agents:
        aid = ca.get("id", "")
        live_agents[aid] = ca

    # Group by project / owner
    projects: Dict[str, Dict[str, Any]] = {}

    for t in tasks:
        proj = t.get("project") or "General"
        if proj not in projects:
            projects[proj] = {"tasks": [], "sessions": {}}
        projects[proj]["tasks"].append(t)

    return {
        "projects": projects,
        "runs": runs,
        "agy_runs": agy_runs,
        "live_agents": live_agents,
    }


def format_tree_text(limit_tasks: int = 5) -> str:
    data = build_hierarchy()
    projects = data["projects"]
    runs = data["runs"]
    agy_runs = data["agy_runs"]
    live_agents = data["live_agents"]

    lines = []
    lines.append("=" * 80)
    lines.append("  MULTI-AGENT LOCAL HARNESS MONITOR (Tier 1 Hypervisor -> Tier 0 Local Network)")
    lines.append("=" * 80)

    # Active resources summary
    vllm_active = sum(
        1 for aid, a in live_agents.items()
        if (a.get("state") in ("working", "running") or a.get("status") in ("working", "running"))
        and runs.get(aid, {}).get("backend", "local") == "local"
    )

    lines.append(
        f"Resources: [vLLM 4x 3090: {vllm_active}/8 active] | "
        f"[Total Native Agents: {len(live_agents)}]"
    )
    lines.append("-" * 80)

    # Tree Structure: Hypervisor (Claude Code / Antigravity / Codex) -> Local Workers
    lines.append("👑 TIER 1: HYPERVISOR & ARCHITECT (Claude Code / Antigravity / Codex)")

    # Group runs by architect / direct
    architects = []
    workers_by_parent: Dict[str, List[Dict[str, Any]]] = {}
    direct_workers = []

    # Sort runs chronologically descending
    sorted_runs = sorted(runs.items(), key=lambda kv: kv[1].get("at", 0), reverse=True)

    for rid, r in sorted_runs:
        r_item = dict(r)
        r_item["id"] = rid
        parent = r.get("parent_id") or "supervisor"
        role = r.get("role")
        backend = r.get("backend")

        if role == "architect" or backend == "gemini":
            architects.append(r_item)
        elif parent != "supervisor" and parent in runs:
            workers_by_parent.setdefault(parent, []).append(r_item)
        else:
            direct_workers.append(r_item)

    now = time.time()

    # 1. Render Local Workers directly reporting to the Hypervisor
    all_primary_workers = direct_workers[:8]
    for i, dw in enumerate(all_primary_workers):
        wid = dw["id"]
        live = live_agents.get(wid, {})
        st = (live.get("state") or live.get("status") or "settled").upper()
        icon = "🟢" if st in ("WORKING", "RUNNING") else ("🟡" if st == "BLOCKED" else "⚪")
        elapsed = round(now - dw.get("at", now), 1)
        model = dw.get("model", "Qwen-27B")
        name = dw.get("name", "worker")
        is_last = (i == len(all_primary_workers) - 1 and not architects and not agy_runs)
        prefix = "  └──" if is_last else "  ├──"
        lines.append(f"{prefix} {icon} 🔨 TIER 0: LOCAL WORKER [{wid}] ({model}) - {st} ({elapsed:.1f}s)")
        lines.append(f"  │      Task: {name}")

    # 2. Render any Sub-Architects or delegated planning runs
    for arch in architects[:3]:
        aid = arch["id"]
        live = live_agents.get(aid, {})
        st = (live.get("state") or live.get("status") or "settled").upper()
        icon = "🟢" if st in ("WORKING", "RUNNING") else ("🟡" if st == "BLOCKED" else "⚪")
        elapsed = round(now - arch.get("at", now), 1)
        model = arch.get("model", "architect")
        name = arch.get("name", "architect")

        lines.append(f"  ├── {icon} 🧠 SUB-ARCHITECT [{aid}] ({model}) - {st} ({elapsed:.1f}s)")
        lines.append(f"  │      Name: {name}")

        children = workers_by_parent.get(aid, [])
        if children:
            for j, ch in enumerate(children[:4]):
                cid = ch["id"]
                clive = live_agents.get(cid, {})
                cst = (clive.get("state") or clive.get("status") or "settled").upper()
                cicon = "🟢" if cst in ("WORKING", "RUNNING") else ("🟡" if cst == "BLOCKED" else "⚪")
                celapsed = round(now - ch.get("at", now), 1)
                cmodel = ch.get("model", "Qwen-27B")
                cname = ch.get("name", "worker")
                pipe = "  │      └──" if j == len(children) - 1 else "  │      ├──"
                lines.append(f"{pipe} {cicon} 🔨 TIER 0: WORKER [{cid}] ({cmodel}) - {cst} ({celapsed:.1f}s)")
                lines.append(f"  │             Task: {cname}")

    # 3. Render any agy background tasks
    for a in agy_runs[:3]:
        st = a.get("status", "unknown").upper()
        rid = a.get("run_id", "")
        model = a.get("model", "gemini")
        elapsed = a.get("elapsed_seconds") or (
            round(time.time() - a.get("start_time", time.time()), 1) if st == "RUNNING" else a.get("agy_duration", 0)
        )
        task_snippet = (a.get("task") or "")[:50].replace("\n", " ")
        icon = "🟢" if st == "COMPLETED" else ("🟡" if st == "RUNNING" else "🔴")
        lines.append(f"  ├── {icon} 🧠 AGY TASK [{rid}] ({model}) - {st} ({elapsed:.1f}s)")
        lines.append(f"  │      Task: \"{task_snippet}\"")

    # Coordination tasks section
    if projects:
        lines.append("-" * 80)
        lines.append("📋 COORDINATION BOARD TASKS (coordination.sqlite3):")
        for proj_name, pdata in list(projects.items())[:2]:
            lines.append(f"  📁 Project: {proj_name}")
            for t in pdata["tasks"][:limit_tasks]:
                t_key = t.get("task_key", "task")
                status = t.get("status", "unknown").upper()
                summary = (t.get("summary") or "")[:60]
                icon = "🟢" if status == "DONE" else ("🟡" if status == "ACTIVE" else "⚪")
                lines.append(f"    └── {icon} Task: [{t_key}] ({status}) - {summary}")

    lines.append("=" * 80)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Multi-agent orchestration hierarchical tree view")
    parser.add_argument("--watch", action="store_true", help="Live watch mode (auto-refresh every 2s)")
    parser.add_argument("--json", action="store_true", help="Output raw JSON hierarchy")
    args = parser.parse_args()

    if args.json:
        print(json.dumps(build_hierarchy(), indent=2, default=str))
        return

    if args.watch:
        try:
            while True:
                os.system("clear" if os.name == "posix" else "cls")
                print(format_tree_text())
                print("\n[Ctrl+C to exit watch mode] Refreshing every 2s...")
                time.sleep(2)
        except KeyboardInterrupt:
            print("\nExiting.")
    else:
        print(format_tree_text())


if __name__ == "__main__":
    main()
