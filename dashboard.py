#!/usr/bin/env python3
"""Web GUI Dashboard for Claude Local Delegate & Multi-Agent Cluster.

Real-time browser observability for:
1. Active & past task board (Kanban / Tree from coordination.sqlite3).
2. Live 4x RTX 3090 GPU cluster telemetry (vLLM & LiteLLM).
3. Quality & Worth-It score matrices, evaluator attribution, and token savings ROI.
4. Tool usage breakdown and error frequency.
5. Task & Run inspector with transcripts and prompts.

Stdlib only (http.server, sqlite3, json). Zero external build/runtime dependencies.
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import cluster_telemetry
import metrics

STATE_DIR = os.environ.get("CLAUDE_LOCAL_DELEGATE_STATE_DIR", os.path.expanduser("~/.claude-local-delegate"))
DB_PATH = os.path.join(STATE_DIR, "coordination.sqlite3")
RUNS_JSON = os.path.join(STATE_DIR, "runs.json")
METRICS_JSONL = os.path.join(STATE_DIR, "metrics.jsonl")


def load_tasks(runs_lookup: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Fetch tasks from coordination.sqlite3 and optionally enrich with run metrics."""
    if not os.path.isfile(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        cursor = conn.cursor()
        cursor.execute("SELECT body FROM tasks ORDER BY rowid DESC;")
        rows = cursor.fetchall()
        tasks = []
        for r in rows:
            try:
                t = json.loads(r[0])
                proj = t.get("project", "")
                t["project_name"] = os.path.basename(proj.rstrip("/")) if proj else ""
                if runs_lookup and t.get("runs") and len(t["runs"]) > 0:
                    r_info = runs_lookup.get(t["runs"][0])
                    if r_info:
                        t["run_speed"] = r_info.get("decode_speed_tok_s")
                        t["run_usd_saved"] = r_info.get("usd_saved")
                        t["run_quality"] = r_info.get("quality")
                        t["run_model"] = r_info.get("model")
                tasks.append(t)
            except Exception:
                pass
        conn.close()
        return tasks
    except Exception:
        return []


def load_runs_map() -> Dict[str, Any]:
    """Load runs metadata from runs.json."""
    if not os.path.isfile(RUNS_JSON):
        return {}
    try:
        with open(RUNS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_metrics_events() -> List[Dict[str, Any]]:
    """Load metrics events from metrics.jsonl."""
    if not os.path.isfile(METRICS_JSONL):
        return []
    events = []
    try:
        with open(METRICS_JSONL, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return events


_TRANSCRIPT_CACHE: Dict[str, Any] = {}


def get_run_stats(run_id: str, rate_meta: Dict[str, Any]) -> Dict[str, Any]:
    """Get stats from rate_meta or resolve on the fly from disk transcript."""
    if rate_meta.get("stats"):
        return rate_meta["stats"]
    if run_id in _TRANSCRIPT_CACHE:
        return _TRANSCRIPT_CACHE[run_id]

    try:
        matches = glob.glob(os.path.expanduser(f"~/.claude/projects/*/*{run_id}*.jsonl"))
        if matches:
            stats = metrics.transcript_stats(matches[0])
            _TRANSCRIPT_CACHE[run_id] = stats
            return stats
    except Exception:
        pass
    return {}


def compile_runs_summary() -> List[Dict[str, Any]]:
    """Merge runs metadata and rated events into a consolidated list."""
    runs_map = load_runs_map()
    events = load_metrics_events()

    spawns = {e["run_id"]: e for e in events if e.get("event") == "spawn" and "run_id" in e}
    rates = {e["run_id"]: e for e in events if e.get("event") == "rate" and "run_id" in e}

    all_run_ids = set(runs_map.keys()) | set(spawns.keys()) | set(rates.keys())
    result = []

    for rid in all_run_ids:
        r_meta = runs_map.get(rid, {})
        s_meta = spawns.get(rid, {})
        rate_meta = rates.get(rid, {})
        stats = get_run_stats(rid, rate_meta)

        at_ts = r_meta.get("at") or s_meta.get("at") or rate_meta.get("at") or 0.0

        dur = stats.get("duration_s")
        out_tokens = stats.get("output_tokens")
        in_tokens = stats.get("total_input_tokens")

        # Backfill speed if missing from historical record
        speed = stats.get("decode_speed_tok_s")
        if speed is None and dur and dur > 0 and out_tokens and out_tokens > 0:
            speed = round(out_tokens / dur, 1)

        # Backfill USD saved if missing from historical record
        usd_saved = stats.get("usd_saved") or 0.0
        tsr = stats.get("token_savings_ratio") or 0.0
        if usd_saved == 0.0 and (in_tokens or out_tokens):
            roi = metrics.calculate_roi(in_tokens or 0, out_tokens or 0)
            usd_saved = roi["usd_saved"]
            tsr = roi["token_savings_ratio"]

        item = {
            "run_id": rid,
            "name": r_meta.get("name") or s_meta.get("name") or "delegate",
            "model": r_meta.get("model") or s_meta.get("model") or stats.get("model") or "qwen-local",
            "role": r_meta.get("role") or s_meta.get("role") or "worker",
            "backend": r_meta.get("backend") or s_meta.get("backend") or "local",
            "profile": r_meta.get("profile_effective") or s_meta.get("profile") or "think",
            "at": at_ts,
            "at_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at_ts)) if at_ts else "-",
            "quality": rate_meta.get("quality"),
            "worth_it": rate_meta.get("worth_it"),
            "evaluator": rate_meta.get("evaluator") or ("human" if rate_meta.get("quality") is not None else None),
            "note": rate_meta.get("note") or "",
            "prompt_chars": s_meta.get("prompt_chars"),
            "prompt_specificity": s_meta.get("prompt_specificity"),
            "duration_s": dur,
            "output_tokens": out_tokens,
            "total_input_tokens": in_tokens,
            "cached_input_tokens": stats.get("cached_input_tokens"),
            "api_calls": stats.get("api_calls"),
            "tool_calls": stats.get("tool_calls"),
            "tool_breakdown": stats.get("tool_breakdown") or {},
            "tool_errors": stats.get("tool_errors", 0),
            "decode_speed_tok_s": speed,
            "usd_saved": usd_saved,
            "token_savings_ratio": tsr,
        }
        result.append(item)

    result.sort(key=lambda x: x["at"], reverse=True)
    return result


def compile_overview() -> Dict[str, Any]:
    """Compile high-level KPIs, cluster health, and tool aggregations."""
    runs = compile_runs_summary()
    runs_lookup = {r["run_id"]: r for r in runs}
    tasks = load_tasks(runs_lookup=runs_lookup)
    telemetry = cluster_telemetry.check_cluster_overview()

    task_counts = {"active": 0, "done": 0, "waiting": 0, "blocked": 0, "cancelled": 0, "total": len(tasks)}
    for t in tasks:
        st = t.get("status", "unknown")
        task_counts[st] = task_counts.get(st, 0) + 1

    rated_runs = [r for r in runs if r["quality"] is not None]
    avg_quality = round(sum(r["quality"] for r in rated_runs) / len(rated_runs), 1) if rated_runs else 0.0
    avg_worth_it = round(sum(r["worth_it"] for r in rated_runs) / len(rated_runs), 1) if rated_runs else 0.0

    total_output_tokens = sum(r["output_tokens"] or 0 for r in runs)
    total_input_tokens = sum(r["total_input_tokens"] or 0 for r in runs)
    total_usd_saved = round(sum(r["usd_saved"] or 0.0 for r in runs), 2)

    # Tool breakdown & error aggregation
    tools_agg: Dict[str, int] = {}
    total_tool_errors = 0
    evaluator_counts: Dict[str, int] = {}

    for r in runs:
        for tname, cnt in r.get("tool_breakdown", {}).items():
            # Simplify Serena/mcp prefixes for readability in chart
            short_name = re.sub(r"^mcp__(?:serena|code-nav)__", "", tname)
            tools_agg[short_name] = tools_agg.get(short_name, 0) + cnt
        total_tool_errors += r.get("tool_errors", 0)

        ev = r.get("evaluator")
        if ev:
            evaluator_counts[ev] = evaluator_counts.get(ev, 0) + 1

    top_tools = sorted(tools_agg.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        "tasks": task_counts,
        "runs_count": len(runs),
        "rated_count": len(rated_runs),
        "avg_quality": avg_quality,
        "avg_worth_it": avg_worth_it,
        "total_output_tokens": total_output_tokens,
        "total_input_tokens": total_input_tokens,
        "total_usd_saved": total_usd_saved,
        "top_tools": dict(top_tools),
        "total_tool_errors": total_tool_errors,
        "evaluator_counts": evaluator_counts,
        "cluster": telemetry,
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Claude Local Delegate — Observability & Metrics Dashboard</title>
  <style>
    :root {
      --bg: #0d1117;
      --card-bg: #161b22;
      --border: #30363d;
      --text: #c9d1d9;
      --text-muted: #8b949e;
      --accent: #58a6ff;
      --green: #3fb950;
      --yellow: #d29922;
      --red: #f85149;
      --purple: #bc8cff;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 20px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 16px;
      margin-bottom: 20px;
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
      gap: 12px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand h1 {
      font-size: 1.4rem;
      font-weight: 600;
      color: #f0f6fc;
    }
    .pills {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      padding: 3px 10px;
      border-radius: 20px;
      font-size: 0.78rem;
      font-weight: 500;
      background: #21262d;
      border: 1px solid var(--border);
    }
    .badge-green { color: var(--green); border-color: rgba(63, 185, 80, 0.4); background: rgba(63, 185, 80, 0.1); }
    .badge-yellow { color: var(--yellow); border-color: rgba(210, 153, 34, 0.4); background: rgba(210, 153, 34, 0.1); }
    .badge-red { color: var(--red); border-color: rgba(248, 81, 73, 0.4); background: rgba(248, 81, 73, 0.1); }
    .badge-purple { color: var(--purple); border-color: rgba(188, 140, 255, 0.4); background: rgba(188, 140, 255, 0.1); }
    .btn {
      background: #238636;
      color: #fff;
      border: none;
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 0.85rem;
      cursor: pointer;
      font-weight: 500;
    }
    .btn:hover { background: #2ea043; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
    }
    .card-title {
      font-size: 0.82rem;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
      margin-bottom: 8px;
    }
    .card-value {
      font-size: 1.8rem;
      font-weight: 700;
      color: #f0f6fc;
    }
    .card-sub {
      font-size: 0.78rem;
      color: var(--text-muted);
      margin-top: 4px;
    }
    .section-title {
      font-size: 1.1rem;
      font-weight: 600;
      margin: 24px 0 12px 0;
      color: #f0f6fc;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .tabs {
      display: flex;
      gap: 8px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 16px;
    }
    .tab-btn {
      background: none;
      border: none;
      color: var(--text-muted);
      font-size: 0.92rem;
      padding: 8px 16px;
      cursor: pointer;
      border-bottom: 2px solid transparent;
    }
    .tab-btn.active {
      color: #f0f6fc;
      border-bottom: 2px solid var(--accent);
      font-weight: 600;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      background: var(--card-bg);
      border-radius: 8px;
      overflow: hidden;
      border: 1px solid var(--border);
    }
    th, td {
      padding: 10px 14px;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }
    th {
      background: #1c2128;
      color: var(--text-muted);
      font-weight: 600;
    }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #21262d; }
    .mono { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
    .kanban-cols {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 16px;
    }
    .kanban-col {
      background: #161b22;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      max-height: 600px;
      overflow-y: auto;
    }
    .kanban-header {
      font-size: 0.85rem;
      font-weight: 600;
      color: var(--text-muted);
      margin-bottom: 12px;
      display: flex;
      justify-content: space-between;
    }
    .task-card {
      background: #21262d;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 12px;
      margin-bottom: 10px;
    }
    .task-summary {
      font-size: 0.88rem;
      font-weight: 500;
      color: #f0f6fc;
      margin-bottom: 6px;
    }
    .task-meta {
      font-size: 0.75rem;
      color: var(--text-muted);
      display: flex;
      justify-content: space-between;
    }
    .tool-bar {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 6px;
      font-size: 0.8rem;
    }
    .tool-bar-bg {
      flex: 1;
      height: 8px;
      background: #21262d;
      border-radius: 4px;
      overflow: hidden;
    }
    .tool-bar-fill {
      height: 100%;
      background: var(--accent);
      border-radius: 4px;
    }
  </style>
</head>
<body>

  <header>
    <div class="brand">
      <h1>⚡ Claude Local Delegate</h1>
      <span class="badge badge-purple">Two-Tiered Orchestrator</span>
    </div>
    <div class="pills">
      <span id="clusterBadge" class="badge">Loading Cluster...</span>
      <span id="savedBadge" class="badge badge-green">$0.00 Saved</span>
      <span id="activeBadge" class="badge">0 Active Tasks</span>
      <button class="btn" onclick="fetchData()">Refresh</button>
    </div>
  </header>

  <!-- Telemetry & Metric Cards -->
  <div class="grid">
    <div class="card">
      <div class="card-title">4x RTX 3090 Cluster</div>
      <div class="card-value" id="vllmStatus">-</div>
      <div class="card-sub" id="vllmDetails">Prefix Cache Hit: -% | KV: -%</div>
    </div>
    <div class="card">
      <div class="card-title">Delegation Quality</div>
      <div class="card-value" id="avgQuality">- / 100</div>
      <div class="card-sub" id="qualityDetails">Avg Worth-It: - / 100</div>
    </div>
    <div class="card">
      <div class="card-title">Economic ROI (Tier 0 vs Tier 1)</div>
      <div class="card-value" id="usdSaved">$0.00</div>
      <div class="card-sub" id="tokenSavings">0 output tokens generated</div>
    </div>
    <div class="card">
      <div class="card-title">Cluster Contention</div>
      <div class="card-value" id="contention">-</div>
      <div class="card-sub" id="contentionSub">Running: 0 | Waiting: 0</div>
    </div>
  </div>

  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('runsTab')">Recent Runs & Performance</button>
    <button class="tab-btn" onclick="switchTab('kanbanTab')">Coordination Kanban Board</button>
    <button class="tab-btn" onclick="switchTab('toolsTab')">Tool Usage & Observability</button>
  </div>

  <!-- Tab 1: Runs Table -->
  <div id="runsTab">
    <table>
      <thead>
        <tr>
          <th>Run ID</th>
          <th>Model / Role</th>
          <th>Task / Name</th>
          <th>Duration</th>
          <th>Tokens (Out/In)</th>
          <th>Speed (tok/s)</th>
          <th>Quality</th>
          <th>Evaluator</th>
          <th>USD Saved</th>
        </tr>
      </thead>
      <tbody id="runsTableBody">
        <tr><td colspan="9" style="text-align: center; color: var(--text-muted);">Loading delegation history...</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Tab 2: Kanban -->
  <div id="kanbanTab" style="display: none;">
    <div class="kanban-cols">
      <div class="kanban-col">
        <div class="kanban-header">
          <span>ACTIVE (<span id="countActive">0</span>)</span>
        </div>
        <div id="tasksActive"></div>
      </div>
      <div class="kanban-col">
        <div class="kanban-header">
          <span>WAITING / BLOCKED (<span id="countWaiting">0</span>)</span>
        </div>
        <div id="tasksWaiting"></div>
      </div>
      <div class="kanban-col">
        <div class="kanban-header">
          <span>DONE (<span id="countDone">0</span>)</span>
        </div>
        <div id="tasksDone"></div>
      </div>
    </div>
  </div>

  <!-- Tab 3: Tools Breakdown -->
  <div id="toolsTab" style="display: none;">
    <div class="card" style="max-width: 600px; margin: 0 auto;">
      <div class="card-title">Top 10 Most Invoked Tools by Delegates</div>
      <div id="toolsBreakdownList" style="margin-top: 14px;"></div>
    </div>
  </div>

  <script>
    function switchTab(tabId) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      event.target.classList.add('active');
      document.getElementById('runsTab').style.display = tabId === 'runsTab' ? 'block' : 'none';
      document.getElementById('kanbanTab').style.display = tabId === 'kanbanTab' ? 'block' : 'none';
      document.getElementById('toolsTab').style.display = tabId === 'toolsTab' ? 'block' : 'none';
    }

    async function fetchData() {
      try {
        const [overviewRes, runsRes, tasksRes] = await Promise.all([
          fetch('/api/overview').then(r => r.json()),
          fetch('/api/runs').then(r => r.json()),
          fetch('/api/tasks').then(r => r.json())
        ]);

        // Render Overview Cards
        const cl = overviewRes.cluster || {};
        const vllm = cl.vllm || {};
        const isHealthy = cl.overall_healthy;
        const clBadge = document.getElementById('clusterBadge');
        clBadge.innerText = isHealthy ? '🟢 4x 3090 Online' : '🔴 Cluster Offline';
        clBadge.className = 'badge ' + (isHealthy ? 'badge-green' : 'badge-red');

        document.getElementById('savedBadge').innerText = '$' + overviewRes.total_usd_saved + ' Saved';
        document.getElementById('activeBadge').innerText = (overviewRes.tasks.active || 0) + ' Active Tasks';

        document.getElementById('vllmStatus').innerText = isHealthy ? 'Healthy' : 'Down';
        document.getElementById('vllmDetails').innerText = 
          'Cache Hit: ' + (vllm.cache_hit_rate_pct || 0) + '% | KV: ' + (vllm.kv_cache_usage_pct || 0) + '%';

        document.getElementById('avgQuality').innerText = overviewRes.avg_quality + ' / 100';
        document.getElementById('qualityDetails').innerText = 'Avg Worth-It: ' + overviewRes.avg_worth_it + ' / 100 (' + overviewRes.rated_count + ' rated)';

        document.getElementById('usdSaved').innerText = '$' + overviewRes.total_usd_saved;
        document.getElementById('tokenSavings').innerText = (overviewRes.total_output_tokens || 0).toLocaleString() + ' output tokens generated';

        document.getElementById('contention').innerText = (vllm.contention || 'None').toUpperCase();
        document.getElementById('contentionSub').innerText = 'Running: ' + (vllm.running_requests || 0) + ' | Waiting: ' + (vllm.waiting_requests || 0);

        // Render Runs
        const tb = document.getElementById('runsTableBody');
        if (runsRes.length === 0) {
          tb.innerHTML = '<tr><td colspan="9" style="text-align: center;">No runs found</td></tr>';
        } else {
          tb.innerHTML = runsRes.slice(0, 30).map(r => `
            <tr>
              <td class="mono"><strong>${r.run_id}</strong></td>
              <td><span class="badge">${r.model}</span> <span style="font-size:0.75rem; color:var(--text-muted);">${r.role}</span></td>
              <td style="max-width: 320px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${r.name}">${r.name}</td>
              <td>${r.duration_s ? r.duration_s + 's' : '-'}</td>
              <td>${r.output_tokens ? r.output_tokens.toLocaleString() + ' / ' + (r.total_input_tokens || 0).toLocaleString() : '-'}</td>
              <td>${r.decode_speed_tok_s ? r.decode_speed_tok_s + ' t/s' : '-'}</td>
              <td>${r.quality !== null ? '<span class="badge badge-green">' + r.quality + '</span>' : '-'}</td>
              <td>${r.evaluator ? '<span class="badge badge-purple">' + r.evaluator + '</span>' : '-'}</td>
              <td style="color: var(--green); font-weight: 500;">${r.usd_saved ? '+$' + r.usd_saved : '-'}</td>
            </tr>
          `).join('');
        }

        // Render Kanban
        const activeTasks = tasksRes.filter(t => t.status === 'active');
        const waitingTasks = tasksRes.filter(t => t.status === 'waiting' || t.status === 'blocked');
        const doneTasks = tasksRes.filter(t => t.status === 'done');

        document.getElementById('countActive').innerText = activeTasks.length;
        document.getElementById('countWaiting').innerText = waitingTasks.length;
        document.getElementById('countDone').innerText = doneTasks.length;

        const renderTaskCard = t => `
          <div class="task-card">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <span class="badge badge-purple" style="font-size:0.7rem;">${t.project_name || 'project'}</span>
              <span class="badge ${t.status === 'done' ? 'badge-green' : (t.status === 'active' ? 'badge-yellow' : '')}" style="font-size:0.7rem;">${t.status}</span>
            </div>
            <div class="task-summary">${t.summary || t.task_key || t.id}</div>
            <div class="task-meta" style="margin-top:6px;">
              <span>${t.runs && t.runs.length ? 'Run: ' + t.runs[0] : 'Pending'}${t.run_model ? ' (' + t.run_model + ')' : ''}</span>
              <span style="color:var(--green); font-weight:500;">${t.run_speed ? t.run_speed + ' t/s' : ''}${t.run_usd_saved ? ' • +$' + t.run_usd_saved : ''}</span>
            </div>
            ${t.run_quality !== null && t.run_quality !== undefined ? `<div style="margin-top:6px;"><span class="badge badge-green" style="font-size:0.72rem;">Quality: ${t.run_quality}/100</span></div>` : ''}
            ${t.note ? `<div style="margin-top:6px; font-size:0.72rem; color:var(--text-muted); font-style:italic;">"${t.note.substring(0, 120)}..."</div>` : ''}
          </div>
        `;

        document.getElementById('tasksActive').innerHTML = activeTasks.slice(0, 10).map(renderTaskCard).join('') || '<div style="color:var(--text-muted);font-size:0.8rem;">No active tasks</div>';
        document.getElementById('tasksWaiting').innerHTML = waitingTasks.slice(0, 10).map(renderTaskCard).join('') || '<div style="color:var(--text-muted);font-size:0.8rem;">None</div>';
        document.getElementById('tasksDone').innerHTML = doneTasks.slice(0, 10).map(renderTaskCard).join('') || '<div style="color:var(--text-muted);font-size:0.8rem;">None</div>';

        // Render Tools Breakdown
        const toolsList = document.getElementById('toolsBreakdownList');
        const topTools = Object.entries(overviewRes.top_tools || {});
        if (topTools.length === 0) {
          toolsList.innerHTML = '<div style="color:var(--text-muted);">No tool metrics recorded yet.</div>';
        } else {
          const maxCnt = topTools[0][1] || 1;
          toolsList.innerHTML = topTools.map(([tname, cnt]) => `
            <div class="tool-bar">
              <span style="width: 140px; text-align: right; font-family: monospace;">${tname}</span>
              <div class="tool-bar-bg">
                <div class="tool-bar-fill" style="width: ${(cnt / maxCnt * 100)}%;"></div>
              </div>
              <span style="width: 40px; color: var(--text-muted);">${cnt}</span>
            </div>
          `).join('');
        }

      } catch (err) {
        console.error('Fetch error:', err);
      }
    }

    // Auto-poll every 4 seconds
    fetchData();
    setInterval(fetchData, 4000);
  </script>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP Handler serving JSON API and Dashboard SPA."""

    def log_message(self, format, *args):
        # Silence default terminal noise during normal polling
        pass

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status: int = 200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self._send_html(HTML_TEMPLATE)
            return

        if path == "/api/overview":
            self._send_json(compile_overview())
            return

        if path == "/api/runs":
            self._send_json(compile_runs_summary())
            return

        if path == "/api/tasks":
            runs = compile_runs_summary()
            runs_lookup = {r["run_id"]: r for r in runs}
            self._send_json(load_tasks(runs_lookup=runs_lookup))
            return

        if path == "/api/telemetry":
            self._send_json(cluster_telemetry.check_cluster_overview())
            return

        self.send_error(404, f"Not Found: {path}")


def run_dashboard(host: str = "127.0.0.1", port: int = 8765):
    server = HTTPServer((host, port), DashboardRequestHandler)
    print(f"[*] Claude Local Delegate Dashboard running at: http://{host}:{port}/")
    print("    Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Dashboard stopped.")
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the delegation metrics web dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Port (default: 8765)")
    args = parser.parse_args()

    run_dashboard(host=args.host, port=args.port)
