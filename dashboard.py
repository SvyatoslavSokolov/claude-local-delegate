#!/usr/bin/env python3
"""Web GUI Dashboard for Claude Local Delegate & Multi-Agent Cluster.

Real-time browser observability for:
1. Active & past task board (Kanban / Tree from coordination.sqlite3) with interactive controls.
2. Live 4x RTX 3090 GPU cluster telemetry (vLLM & LiteLLM).
3. Quality & Worth-It score matrices, evaluator attribution, and token savings ROI.
4. Tool usage breakdown and error frequency.
5. Task & Run inspector with transcripts and prompts.
6. Task state transitions: Pause, Resume, Mark Done, Move to Trash, and Bulk Stale Cleanup.

Stdlib only (http.server, sqlite3, json). Zero external build/runtime dependencies.
"""

import argparse
import glob
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import cluster_telemetry
import metrics

STATE_DIR = os.environ.get("CLAUDE_LOCAL_DELEGATE_STATE_DIR", os.path.expanduser("~/.claude-local-delegate"))
DB_PATH = os.path.join(STATE_DIR, "coordination.sqlite3")
RUNS_JSON = os.path.join(STATE_DIR, "runs.json")
METRICS_JSONL = os.path.join(STATE_DIR, "metrics.jsonl")
CODEX_STATE_DIR = os.path.join(STATE_DIR, "codex_runs")

CODEX_BIN = os.environ.get("CODEX_BIN", shutil.which("codex") or "codex")
AGY_BIN = os.environ.get("AGY_BIN", shutil.which("agy") or "agy")
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", shutil.which("claude") or "claude")


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
                t["project_name"] = os.path.basename(proj.rstrip("/")) if proj else "global"
                if runs_lookup and t.get("runs") and len(t["runs"]) > 0:
                    r_info = runs_lookup.get(t["runs"][0])
                    if r_info:
                        t["run_speed"] = r_info.get("decode_speed_tok_s")
                        t["run_usd_saved"] = r_info.get("usd_saved")
                        t["run_quality"] = r_info.get("quality")
                if t.get("adapter") == "agy" or (t.get("runs") and any(r.startswith("agy-") for r in t.get("runs", []))):
                    agy_rid = next((r for r in t.get("runs", []) if r.startswith("agy-")), None)
                    if agy_rid:
                        agy_cli_log = os.path.expanduser(f"~/.claude-local-delegate/agy_runs/{agy_rid}.cli.log")
                        agy_out = os.path.expanduser(f"~/.claude-local-delegate/agy_runs/{agy_rid}.out")
                        agy_json = os.path.expanduser(f"~/.claude-local-delegate/agy_runs/{agy_rid}.json")
                        cid = None
                        # 1. Try cli log first (available early while running)
                        if os.path.isfile(agy_cli_log):
                            try:
                                import re
                                with open(agy_cli_log, "r", encoding="utf-8", errors="replace") as clf:
                                    m = re.search(r"Created conversation ([0-9a-fA-F-]+)", clf.read())
                                    if m:
                                        cid = m.group(1)
                            except Exception:
                                pass
                        # 2. Try out file if finished
                        if not cid and os.path.isfile(agy_out):
                            try:
                                with open(agy_out, "r", encoding="utf-8") as of:
                                    for line in reversed(of.readlines()):
                                        line = line.strip()
                                        if line.startswith("{") and line.endswith("}"):
                                            p_data = json.loads(line)
                                            if p_data.get("conversation_id"):
                                                cid = p_data.get("conversation_id")
                                                break
                            except Exception:
                                pass
                        # 3. Try json metadata
                        run_st = None
                        run_err = None
                        if os.path.isfile(agy_json):
                            try:
                                with open(agy_json, "r", encoding="utf-8") as af:
                                    ameta = json.load(af)
                                    if not cid:
                                        cid = ameta.get("conversation_id")
                                    run_st = ameta.get("status")
                                    run_err = ameta.get("error")
                            except Exception:
                                pass
                        t["run_status"] = run_st
                        t["run_error"] = run_err
                        if cid:
                            t["conversation_id"] = cid
                            t["attach_command"] = f"agy --conversation {cid}"
                        else:
                            t["conversation_id"] = None
                            t["attach_command"] = None

                        if run_st == "failed" and t.get("status") == "active":
                            t["status"] = "cancelled"
                            clean_err = (run_err or "Process exited with failure").strip().replace("\n", " ")[:150]
                            t["note"] = f"[FAILED: {clean_err}]"
                            try:
                                update_task_status(t["id"], "cancelled", note=f"Auto-cancelled: {clean_err}")
                            except Exception:
                                pass
                tasks.append(t)
            except Exception:
                pass
        conn.close()
        return tasks
    except Exception:
        return []


def update_task_status(task_id: str, new_status: str, note: str = "") -> Tuple[bool, Optional[str]]:
    """Update status of a single task in coordination.sqlite3."""
    if new_status not in ("active", "paused", "waiting", "done", "cancelled"):
        return False, f"Invalid status: {new_status}"
    if not os.path.isfile(DB_PATH):
        return False, "Database not found"
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE;")
        c.execute("SELECT rowid, body FROM tasks WHERE id = ?;", (task_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return False, "Task not found"
        rowid, body_str = row
        body = json.loads(body_str)
        old_status = body.get("status")
        body["status"] = new_status
        body["updated_at"] = time.time()
        if note:
            body["note"] = f"{body.get('note', '')} [{new_status.upper()}: {note}]".strip()
        c.execute("UPDATE tasks SET body = ? WHERE rowid = ?;", (json.dumps(body), rowid))
        event_payload = {
            "task_id": task_id,
            "owner": "dashboard",
            "kind": "status_change",
            "text": f"Status updated from {old_status} to {new_status} via Dashboard",
            "at": time.time(),
        }
        c.execute("INSERT INTO events (project, body) VALUES (?, ?);", (body.get("project", ""), json.dumps(event_payload)))
        conn.commit()
        conn.close()
        return True, None
    except Exception as exc:
        return False, str(exc)


def cleanup_stale_tasks(max_age_hours: float = 2.0, target_status: str = "done") -> int:
    """Find active/waiting/paused tasks older than max_age_hours and mark them target_status."""
    if target_status not in ("done", "cancelled"):
        target_status = "done"
    if not os.path.isfile(DB_PATH):
        return 0
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE;")
        c.execute("SELECT rowid, id, body FROM tasks;")
        rows = c.fetchall()
        now = time.time()
        cutoff = now - (max_age_hours * 3600)
        updated_count = 0
        for rowid, tid, body_str in rows:
            try:
                body = json.loads(body_str)
                st = body.get("status")
                if st in ("active", "waiting", "paused"):
                    created = body.get("created_at") or 0.0
                    updated = body.get("updated_at") or created
                    if updated < cutoff:
                        body["status"] = target_status
                        body["updated_at"] = now
                        body["note"] = f"{body.get('note', '')} [AUTO-CLEANUP: Marked {target_status} by dashboard]".strip()
                        c.execute("UPDATE tasks SET body = ? WHERE rowid = ?;", (json.dumps(body), rowid))
                        event_payload = {
                            "task_id": tid,
                            "owner": "dashboard",
                            "kind": "status_change",
                            "text": f"Auto-cleaned from {st} to {target_status} via Dashboard",
                            "at": now,
                        }
                        c.execute("INSERT INTO events (project, body) VALUES (?, ?);", (body.get("project", ""), json.dumps(event_payload)))
                        updated_count += 1
            except Exception:
                pass
        conn.commit()
        conn.close()
        return updated_count
    except Exception:
        return 0


def spawn_task_from_dashboard(
    adapter: str,
    task: str,
    model: Optional[str] = None,
    cwd: Optional[str] = None,
    summary: Optional[str] = None,
) -> Tuple[bool, Dict[str, Any]]:
    """Spawn a task via local worker, agy, architect, or codex, and register in coordination.sqlite3."""
    if not task or not task.strip():
        return False, {"error": "Task prompt cannot be empty"}

    adapter = (adapter or "local").strip().lower()
    if adapter not in ("local", "claude", "agy", "architect", "codex"):
        return False, {"error": f"Unsupported adapter: {adapter}. Must be local, claude, agy, architect, or codex."}

    resolved_cwd = os.path.abspath(os.path.expanduser(cwd or os.getcwd()))
    if not os.path.isdir(resolved_cwd):
        return False, {"error": f"Working directory does not exist: {cwd}"}

    task_text = task.strip()
    summary_text = (summary or task_text.split("\n", 1)[0])[:120].strip()

    run_id = None
    try:
        if adapter in ("local", "architect"):
            import server
            role = "architect" if adapter == "architect" else "worker"
            agent_persona = "gemini-architect" if adapter == "architect" else None
            allowed_tools = None if adapter == "architect" else server.DEFAULT_ALLOWED_TOOLS
            name = server._format_agent_name(None, task_text, role=role, profile="think" if role == "worker" else None)
            short_id, err = server._spawn_native_agent(
                task=task_text,
                allowed_tools=allowed_tools,
                cwd=resolved_cwd,
                name=name,
                permission_mode=server.DEFAULT_PERMISSION_MODE,
                agent=agent_persona,
                spawn_model=model or None,
                role=role,
            )
            if err:
                return False, {"error": err}
            run_id = short_id

        elif adapter == "claude":
            # Direct Claude Code on user's subscription (Anthropic Cloud)
            cmd = [
                CLAUDE_BIN, "--bg",
                "--permission-mode", "bypassPermissions",
            ]
            if model:
                cmd.extend(["--model", model])
            effective_task = f"[PROJECT CWD: {resolved_cwd}]\n\n" + task_text
            cmd.append(effective_task)
            proc = subprocess.run(
                cmd,
                cwd=resolved_cwd,
                env=dict(os.environ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            out = proc.stdout.decode("utf-8", "replace") if proc.stdout else ""
            if proc.returncode != 0 and "backgrounded" not in out:
                return False, {"error": f"claude --bg exited {proc.returncode}.\n{out.strip()[-1000:]}"}
            import server
            short_id = server._parse_bg_id(out)
            run_id = short_id or f"claude-{uuid.uuid4().hex[:6]}"

        elif adapter == "agy":
            from adapters.agy.agy_delegate import AgyDelegateManager
            manager = AgyDelegateManager()
            meta = manager.spawn(
                task=task_text,
                cwd=resolved_cwd,
                model=model or None,
            )
            run_id = meta.get("run_id")
            agy_conv_id = None
            st = {}
            for _ in range(12):
                time.sleep(0.25)
                st = manager.check_status(run_id)
                agy_conv_id = st.get("conversation_id")
                if agy_conv_id or st.get("status") in ("failed", "completed"):
                    break
            if st.get("status") == "failed":
                err_msg = st.get("error") or "AGY process failed to start."
                return False, {"error": err_msg}
            agy_attach_cmd = f"agy --conversation {agy_conv_id}" if agy_conv_id else None

        elif adapter == "codex":
            os.makedirs(CODEX_STATE_DIR, exist_ok=True)
            run_id = f"codex-{uuid.uuid4().hex[:8]}"
            out_f = open(os.path.join(CODEX_STATE_DIR, f"{run_id}.out"), "w", encoding="utf-8")
            err_f = open(os.path.join(CODEX_STATE_DIR, f"{run_id}.err"), "w", encoding="utf-8")
            cmd = [CODEX_BIN, "exec"]
            if model:
                cmd.extend(["-c", f'model="{model}"'])
            cmd.append(task_text)
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=resolved_cwd,
                    stdout=out_f,
                    stderr=err_f,
                    start_new_session=True,
                )
            except Exception as exc:
                out_f.close()
                err_f.close()
                return False, {"error": f"Failed to spawn codex: {exc}"}

            meta_data = {
                "run_id": run_id,
                "pid": proc.pid,
                "task": task_text,
                "cwd": resolved_cwd,
                "model": model or "default",
                "start_time": time.time(),
                "status": "running",
            }
            with open(os.path.join(CODEX_STATE_DIR, f"{run_id}.json"), "w", encoding="utf-8") as mf:
                json.dump(meta_data, mf)

    except Exception as exc:
        return False, {"error": f"Exception spawning {adapter}: {exc}"}

    # Register task on Coordination Board
    task_id = None
    try:
        from coordination import Board
        board = Board(DB_PATH)
        task_key = f"web-{adapter}-{uuid.uuid4().hex[:6]}"
        claim_res = board.claim("dashboard", {
            "project": resolved_cwd,
            "task_key": task_key,
            "mode": "write",
            "summary": summary_text,
        })
        task_obj = claim_res.get("task", {})
        task_id = task_obj.get("id")
        if task_id:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT rowid, body FROM tasks WHERE id = ?;", (task_id,))
            row = c.fetchone()
            if row:
                rowid, b_str = row
                body = json.loads(b_str)
                if run_id:
                    body["runs"] = [run_id]
                body["adapter"] = adapter
                body["model"] = model or "default"
                if adapter == "agy":
                    if agy_conv_id:
                        body["conversation_id"] = agy_conv_id
                        body["attach_command"] = agy_attach_cmd
                    else:
                        body["conversation_id"] = None
                        body["attach_command"] = None
                body["note"] = f"Spawned via Web Dashboard ({adapter}: {model or 'default'})"
                c.execute("UPDATE tasks SET body = ? WHERE rowid = ?;", (json.dumps(body), rowid))
                conn.commit()
            conn.close()
    except Exception:
        pass

    res_dict = {
        "run_id": run_id,
        "task_id": task_id,
        "adapter": adapter,
        "model": model or "default",
        "cwd": resolved_cwd,
        "summary": summary_text,
    }
    if adapter == "agy":
        res_dict["conversation_id"] = agy_conv_id
        res_dict["attach_command"] = agy_attach_cmd
    return True, res_dict


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


def get_known_projects() -> List[Dict[str, str]]:
    """Discover known projects across SQLite coordination, Claude directories, and Gemini."""
    found: Dict[str, str] = {}

    cur = os.getcwd()
    found[cur] = os.path.basename(cur.rstrip("/"))

    if os.path.isfile(DB_PATH):
        try:
            conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            for row in conn.cursor().execute("SELECT body FROM tasks;"):
                try:
                    b = json.loads(row[0])
                    p = b.get("project")
                    if p and os.path.isdir(p):
                        found[p] = os.path.basename(p.rstrip("/"))
                except Exception:
                    pass
            conn.close()
        except Exception:
            pass

    gp = os.path.expanduser("~/.gemini/projects.json")
    if os.path.isfile(gp):
        try:
            with open(gp, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for p in data:
                        if isinstance(p, str) and os.path.isdir(p):
                            found[p] = os.path.basename(p.rstrip("/"))
        except Exception:
            pass

    res = [{"name": name, "path": path} for path, name in found.items()]
    res.sort(key=lambda x: (x["path"] != cur, x["name"].lower()))
    return res


def compile_quotas() -> Dict[str, Any]:
    """Calculate rolling 5-hour and 7-day limits for Claude Code, AGY, Codex, and Local Cluster."""
    now_s = time.time()
    five_h_ago = now_s - (5 * 3600)
    one_w_ago = now_s - (7 * 86400)

    def _calc_quota(path: str, ts_multiplier: float, limit_5h: int, limit_7d: int, label: str) -> Dict[str, Any]:
        timestamps_5h = []
        cnt_7d = 0
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            d = json.loads(line)
                            ts = d.get("timestamp") or d.get("ts")
                            if ts:
                                ts_s = float(ts) / ts_multiplier
                                if ts_s >= one_w_ago:
                                    cnt_7d += 1
                                    if ts_s >= five_h_ago:
                                        timestamps_5h.append(ts_s)
                        except Exception:
                            pass
            except Exception:
                pass
        cnt_5h = len(timestamps_5h)
        rem_5h = max(0, limit_5h - cnt_5h) if limit_5h > 0 else 0
        pct_5h = round((rem_5h / limit_5h) * 100, 1) if limit_5h > 0 else 0.0

        rem_7d = max(0, limit_7d - cnt_7d) if limit_7d > 0 else 0
        pct_7d = round((rem_7d / limit_7d) * 100, 1) if limit_7d > 0 else 0.0

        if timestamps_5h:
            earliest = min(timestamps_5h)
            reset_in_s = max(0, int((earliest + 5 * 3600) - now_s))
            h = reset_in_s // 3600
            m = (reset_in_s % 3600) // 60
            reset_str = f"{h}h {m}m"
        else:
            reset_str = "Full Quota" if limit_5h > 0 else "--"

        status = "healthy" if (limit_5h > 0 and pct_5h > 35) else ("warning" if (limit_5h > 0 and pct_5h > 15) else ("healthy" if limit_5h == 0 else "danger"))

        return {
            "label": label,
            "used_5h": cnt_5h,
            "limit_5h": limit_5h,
            "rem_5h": rem_5h,
            "pct_5h": pct_5h,
            "used_7d": cnt_7d,
            "limit_7d": limit_7d,
            "rem_7d": rem_7d,
            "pct_7d": pct_7d,
            "reset_in": reset_str,
            "status": status,
        }

    claude_hist = os.path.expanduser("~/.claude/history.jsonl")
    agy_hist = os.path.expanduser("~/.gemini/antigravity-cli/history.jsonl")
    codex_hist = os.path.expanduser("~/.codex/history.jsonl")

    claude_quota = _calc_quota(claude_hist, ts_multiplier=1000.0, limit_5h=0, limit_7d=0, label="Claude Code (Pro)")
    agy_quota = _calc_quota(agy_hist, ts_multiplier=1000.0, limit_5h=0, limit_7d=0, label="Google Antigravity (Gemini)")
    codex_quota = _calc_quota(codex_hist, ts_multiplier=1.0, limit_5h=0, limit_7d=0, label="OpenAI Codex")

    local_quota = {
        "label": "Local Cluster (4x RTX 3090)",
        "used_5h": 0,
        "limit_5h": "∞",
        "rem_5h": "∞",
        "pct_5h": 100.0,
        "used_7d": 0,
        "limit_7d": "∞",
        "rem_7d": "∞",
        "pct_7d": 100.0,
        "reset_in": "Unlimited (Local GPU)",
        "status": "healthy",
    }

    return {
        "claude": claude_quota,
        "agy": agy_quota,
        "codex": codex_quota,
        "local": local_quota,
        "architect": agy_quota,
    }


def compile_overview() -> Dict[str, Any]:
    """Compile high-level KPIs, cluster health, and tool aggregations."""
    runs = compile_runs_summary()
    runs_lookup = {r["run_id"]: r for r in runs}
    tasks = load_tasks(runs_lookup=runs_lookup)
    telemetry = cluster_telemetry.check_cluster_overview()

    task_counts = {"active": 0, "done": 0, "waiting": 0, "paused": 0, "cancelled": 0, "total": len(tasks)}
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
        "quotas": compile_quotas(),
        "known_projects": get_known_projects(),
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
      transition: opacity 0.15s ease;
    }
    .btn:hover { opacity: 0.9; }
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
    
    .kanban-toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 10px 14px;
      flex-wrap: wrap;
      gap: 10px;
    }
    .select-ctrl {
      background: #21262d;
      border: 1px solid var(--border);
      color: #f0f6fc;
      border-radius: 6px;
      padding: 5px 10px;
      font-size: 0.82rem;
      outline: none;
    }
    .kanban-cols {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
    }
    .kanban-col {
      background: #161b22;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      max-height: 700px;
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
      transition: transform 0.1s ease;
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
    .task-actions {
      display: flex;
      gap: 6px;
      margin-top: 10px;
      padding-top: 8px;
      border-top: 1px solid rgba(48, 54, 61, 0.6);
      flex-wrap: wrap;
    }
    .task-act-btn {
      background: #21262d;
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 4px;
      font-size: 0.72rem;
      padding: 3px 8px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      transition: all 0.15s ease;
    }
    .task-act-btn:hover { background: #30363d; color: #fff; }
    .task-act-green:hover { background: rgba(63, 185, 80, 0.2); border-color: var(--green); color: var(--green); }
    .task-act-red:hover { background: rgba(248, 81, 73, 0.2); border-color: var(--red); color: var(--red); }
    .task-act-blue:hover { background: rgba(88, 166, 255, 0.2); border-color: var(--accent); color: var(--accent); }
    .task-act-yellow:hover { background: rgba(210, 153, 34, 0.2); border-color: var(--yellow); color: var(--yellow); }

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

    .badge-blue { color: var(--accent); border-color: rgba(88, 166, 255, 0.4); background: rgba(88, 166, 255, 0.1); }

    /* Modal Styles */
    .modal-backdrop {
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(4px);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 9999;
    }
    .modal-box {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      width: 580px;
      max-width: 94vw;
      max-height: 90vh;
      overflow-y: auto;
      box-shadow: 0 20px 45px rgba(0, 0, 0, 0.85);
      display: flex;
      flex-direction: column;
    }
    .modal-header {
      padding: 16px 20px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .modal-header h2 {
      font-size: 1.1rem;
      margin: 0;
      color: #fff;
    }
    .close-btn {
      background: none;
      border: none;
      color: var(--text-muted);
      font-size: 1.4rem;
      cursor: pointer;
    }
    .close-btn:hover { color: #fff; }
    .modal-body {
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .form-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .form-group label {
      font-size: 0.8rem;
      color: var(--text-muted);
      font-weight: 500;
    }
    .form-ctrl {
      background: #0d1117;
      border: 1px solid var(--border);
      color: #c9d1d9;
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 0.85rem;
      font-family: inherit;
    }
    .form-ctrl:focus {
      border-color: var(--accent);
      outline: none;
    }
    .modal-footer {
      padding: 14px 20px;
      border-top: 1px solid var(--border);
      display: flex;
      justify-content: flex-end;
      gap: 10px;
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
      <span id="claudeQuotaPill" class="badge badge-purple" style="font-size:0.75rem;">Claude 5h: --</span>
      <span id="savedBadge" class="badge badge-green">$0.00 Saved</span>
      <span id="activeBadge" class="badge">0 Active Tasks</span>
      <button class="btn" onclick="openSpawnModal()" style="background: linear-gradient(135deg, #1f6feb, #238636); border: none; font-weight: 600; display: inline-flex; align-items: center; gap: 6px;">➕ New Task / Dispatch</button>
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
    <button class="tab-btn active" onclick="switchTab('kanbanTab')">Coordination Kanban Board</button>
    <button class="tab-btn" onclick="switchTab('runsTab')">Recent Runs & Performance</button>
    <button class="tab-btn" onclick="switchTab('toolsTab')">Tool Usage & Observability</button>
    <button class="tab-btn" onclick="switchTab('quotasTab')">Architect Limits & Quotas</button>
  </div>

  <!-- Tab 1: Kanban -->
  <div id="kanbanTab">
    <div class="kanban-toolbar">
      <div style="display: flex; align-items: center; gap: 10px;">
        <label style="font-size: 0.85rem; color: var(--text-muted);">Filter Project:</label>
        <select id="projectFilter" class="select-ctrl" onchange="filterKanban()">
          <option value="all">All Projects</option>
        </select>
      </div>
      <div style="display: flex; gap: 8px;">
        <button class="btn" style="background: #1f6feb; border: none; font-size: 0.8rem; font-weight: 600;" onclick="openSpawnModal()">
          🚀 Dispatch Task
        </button>
        <button class="btn" style="background: #21262d; border: 1px solid var(--border); font-size: 0.8rem;" onclick="cleanupStale('done')">
          🧹 Archive Stale as Done
        </button>
        <button class="btn" style="background: rgba(248, 81, 73, 0.15); border: 1px solid rgba(248, 81, 73, 0.4); color: var(--red); font-size: 0.8rem;" onclick="cleanupStale('cancelled')">
          🗑️ Move Stale to Trash
        </button>
      </div>
    </div>

    <div class="kanban-cols">
      <div class="kanban-col">
        <div class="kanban-header">
          <span>ACTIVE (<span id="countActive">0</span>)</span>
        </div>
        <div id="tasksActive"></div>
      </div>
      <div class="kanban-col">
        <div class="kanban-header">
          <span>PAUSED / WAITING (<span id="countWaiting">0</span>)</span>
        </div>
        <div id="tasksWaiting"></div>
      </div>
      <div class="kanban-col">
        <div class="kanban-header">
          <span>DONE (<span id="countDone">0</span>)</span>
        </div>
        <div id="tasksDone"></div>
      </div>
      <div class="kanban-col">
        <div class="kanban-header">
          <span>TRASH / CANCELLED (<span id="countCancelled">0</span>)</span>
        </div>
        <div id="tasksCancelled"></div>
      </div>
    </div>
  </div>

  <!-- Tab 2: Runs Table -->
  <div id="runsTab" style="display: none;">
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

  <!-- Tab 3: Tools Breakdown -->
  <div id="toolsTab" style="display: none;">
    <div class="card" style="max-width: 600px; margin: 0 auto;">
      <div class="card-title">Top 10 Most Invoked Tools by Delegates</div>
      <div id="toolsBreakdownList" style="margin-top: 14px;"></div>
    </div>
  </div>

  <!-- Tab 4: Architect Limits & Quotas -->
  <div id="quotasTab" style="display: none;">
    <div style="margin-bottom: 16px;">
      <h2 style="font-size: 1.15rem; margin: 0 0 6px 0;">Architect Super-Model Quotas & Rolling Rate Limits</h2>
      <p style="color: var(--text-muted); font-size: 0.85rem; margin: 0;">
        Track remaining 5-hour rolling windows, weekly allowances, and time-to-reset across Claude Code, Google Antigravity, OpenAI Codex, and the local GPU cluster.
      </p>
    </div>

    <div class="grid" style="grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));">
      <!-- Claude Code Card -->
      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
          <span style="font-weight: 600; font-size: 1rem; color: #fff;">⚡ Claude Code</span>
          <span class="badge badge-purple">Anthropic Pro</span>
        </div>
        <div style="margin-top: 14px;">
          <div style="display: flex; justify-content: space-between; font-size: 0.85rem; margin-bottom: 4px;">
            <span>5-Hour Rolling Limit</span>
            <span style="font-weight: 600;"><span id="claude5hRem">0</span> / <span id="claude5hLimit">0</span> left</span>
          </div>
          <div class="tool-bar-bg">
            <div id="claude5hBar" class="tool-bar-fill" style="width: 100%; background: var(--green);"></div>
          </div>
          <div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">
            <span>Used: <span id="claude5hUsed">--</span></span>
            <span>Resets in: <span id="claude5hReset" style="color: var(--accent); font-weight: 500;">--</span></span>
          </div>
        </div>

        <div style="margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border);">
          <div style="display: flex; justify-content: space-between; font-size: 0.85rem; margin-bottom: 4px;">
            <span>7-Day Weekly Limit</span>
            <span style="font-weight: 600;"><span id="claude7dRem">0</span> / <span id="claude7dLimit">0</span> left</span>
          </div>
          <div class="tool-bar-bg">
            <div id="claude7dBar" class="tool-bar-fill" style="width: 100%; background: var(--accent);"></div>
          </div>
          <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">
            Used this week: <span id="claude7dUsed">--</span> prompts
          </div>
        </div>
      </div>

      <!-- Google Antigravity Card -->
      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
          <span style="font-weight: 600; font-size: 1rem; color: #fff;">🧠 Google Antigravity</span>
          <span class="badge badge-blue">Gemini 2.5/3.8</span>
        </div>
        <div style="margin-top: 14px;">
          <div style="display: flex; justify-content: space-between; font-size: 0.85rem; margin-bottom: 4px;">
            <span>5-Hour Rolling Limit</span>
            <span style="font-weight: 600;"><span id="agy5hRem">0</span> / <span id="agy5hLimit">0</span> left</span>
          </div>
          <div class="tool-bar-bg">
            <div id="agy5hBar" class="tool-bar-fill" style="width: 100%; background: var(--green);"></div>
          </div>
          <div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">
            <span>Used: <span id="agy5hUsed">--</span></span>
            <span>Resets in: <span id="agy5hReset" style="color: var(--accent); font-weight: 500;">--</span></span>
          </div>
        </div>

        <div style="margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border);">
          <div style="display: flex; justify-content: space-between; font-size: 0.85rem; margin-bottom: 4px;">
            <span>7-Day Weekly Limit</span>
            <span style="font-weight: 600;"><span id="agy7dRem">0</span> / <span id="agy7dLimit">0</span> left</span>
          </div>
          <div class="tool-bar-bg">
            <div id="agy7dBar" class="tool-bar-fill" style="width: 100%; background: var(--accent);"></div>
          </div>
          <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">
            Used this week: <span id="agy7dUsed">--</span> requests
          </div>
        </div>
      </div>

      <!-- OpenAI Codex Card -->
      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
          <span style="font-weight: 600; font-size: 1rem; color: #fff;">🤖 OpenAI Codex</span>
          <span class="badge" style="border-color: #10a37f; color: #10a37f;">o3-mini / GPT-4o</span>
        </div>
        <div style="margin-top: 14px;">
          <div style="display: flex; justify-content: space-between; font-size: 0.85rem; margin-bottom: 4px;">
            <span>5-Hour Rolling Limit</span>
            <span style="font-weight: 600;"><span id="codex5hRem">0</span> / <span id="codex5hLimit">0</span> left</span>
          </div>
          <div class="tool-bar-bg">
            <div id="codex5hBar" class="tool-bar-fill" style="width: 100%; background: var(--green);"></div>
          </div>
          <div style="display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">
            <span>Used: <span id="codex5hUsed">--</span></span>
            <span>Resets in: <span id="codex5hReset" style="color: var(--accent); font-weight: 500;">--</span></span>
          </div>
        </div>

        <div style="margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border);">
          <div style="display: flex; justify-content: space-between; font-size: 0.85rem; margin-bottom: 4px;">
            <span>7-Day Weekly Limit</span>
            <span style="font-weight: 600;"><span id="codex7dRem">0</span> / <span id="codex7dLimit">0</span> left</span>
          </div>
          <div class="tool-bar-bg">
            <div id="codex7dBar" class="tool-bar-fill" style="width: 100%; background: var(--accent);"></div>
          </div>
          <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">
            Used this week: <span id="codex7dUsed">--</span> requests
          </div>
        </div>
      </div>

      <!-- Local Cluster Card -->
      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
          <span style="font-weight: 600; font-size: 1rem; color: #fff;">🖥️ Local Cluster (vLLM)</span>
          <span class="badge badge-green">4x RTX 3090</span>
        </div>
        <div style="margin-top: 14px;">
          <div style="display: flex; justify-content: space-between; font-size: 0.85rem; margin-bottom: 4px;">
            <span>5-Hour Rolling Limit</span>
            <span style="font-weight: 600; color: var(--green);">∞ Unlimited</span>
          </div>
          <div class="tool-bar-bg">
            <div class="tool-bar-fill" style="width: 100%; background: var(--green);"></div>
          </div>
          <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">
            Zero rate limits • Free local inference
          </div>
        </div>

        <div style="margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border);">
          <div style="display: flex; justify-content: space-between; font-size: 0.85rem; margin-bottom: 4px;">
            <span>7-Day Weekly Limit</span>
            <span style="font-weight: 600; color: var(--green);">∞ Unlimited</span>
          </div>
          <div class="tool-bar-bg">
            <div class="tool-bar-fill" style="width: 100%; background: var(--green);"></div>
          </div>
          <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 4px;">
            Prefix cache active • 93.8% hit rate
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
    let rawTasks = [];
    let rawQuotas = null;
    let rawProjects = [];
    let currentFilter = 'all';

    function copyCmd(text, btn) {
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(() => {
          showCopied(btn);
        }).catch(() => {
          fallbackCopy(text, btn);
        });
        return;
      }
      fallbackCopy(text, btn);
    }

    function fallbackCopy(text, btn) {
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.left = '-9999px';
        ta.style.top = '0';
        document.body.appendChild(ta);
        ta.focus();
        ta.select();
        const ok = document.execCommand('copy');
        document.body.removeChild(ta);
        if (ok) {
          showCopied(btn);
          return;
        }
      } catch (e) {}
      prompt('Copy command (Ctrl+C, Enter):', text);
    }

    function showCopied(btn) {
      if (!btn) return;
      const originalText = btn.innerText;
      btn.innerText = '✅ Copied!';
      btn.style.background = '#1f6feb';
      setTimeout(() => {
        btn.innerText = originalText;
        btn.style.background = '#238636';
      }, 2000);
    }

    function switchTab(tabId) {
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      event.target.classList.add('active');
      document.getElementById('runsTab').style.display = tabId === 'runsTab' ? 'block' : 'none';
      document.getElementById('kanbanTab').style.display = tabId === 'kanbanTab' ? 'block' : 'none';
      document.getElementById('toolsTab').style.display = tabId === 'toolsTab' ? 'block' : 'none';
      document.getElementById('quotasTab').style.display = tabId === 'quotasTab' ? 'block' : 'none';
    }

    function filterKanban() {
      currentFilter = document.getElementById('projectFilter').value;
      renderKanban();
    }

    function renderKanban() {
      let filtered = rawTasks;
      if (currentFilter !== 'all') {
        filtered = rawTasks.filter(t => t.project_name === currentFilter);
      }

      const activeTasks = filtered.filter(t => t.status === 'active');
      const waitingTasks = filtered.filter(t => t.status === 'waiting' || t.status === 'paused');
      const doneTasks = filtered.filter(t => t.status === 'done');
      const cancelledTasks = filtered.filter(t => t.status === 'cancelled');

      document.getElementById('countActive').innerText = activeTasks.length;
      document.getElementById('countWaiting').innerText = waitingTasks.length;
      document.getElementById('countDone').innerText = doneTasks.length;
      document.getElementById('countCancelled').innerText = cancelledTasks.length;

      const renderTaskCard = t => {
        let actionBtns = '';
        if (t.status === 'active') {
          actionBtns = `
            <button class="task-act-btn task-act-yellow" onclick="updateTask('${t.id}', 'paused')">⏸️ Pause</button>
            <button class="task-act-btn task-act-green" onclick="updateTask('${t.id}', 'done')">✅ Done</button>
            <button class="task-act-btn task-act-red" onclick="updateTask('${t.id}', 'cancelled')">🗑️ Trash</button>
          `;
        } else if (t.status === 'paused' || t.status === 'waiting') {
          actionBtns = `
            <button class="task-act-btn task-act-blue" onclick="updateTask('${t.id}', 'active')">▶️ Resume</button>
            <button class="task-act-btn task-act-green" onclick="updateTask('${t.id}', 'done')">✅ Done</button>
            <button class="task-act-btn task-act-red" onclick="updateTask('${t.id}', 'cancelled')">🗑️ Trash</button>
          `;
        } else if (t.status === 'done') {
          actionBtns = `
            <button class="task-act-btn task-act-blue" onclick="updateTask('${t.id}', 'active')">🔄 Reopen</button>
            <button class="task-act-btn task-act-red" onclick="updateTask('${t.id}', 'cancelled')">🗑️ Trash</button>
          `;
        } else if (t.status === 'cancelled') {
          actionBtns = `
            <button class="task-act-btn task-act-blue" onclick="updateTask('${t.id}', 'active')">🔄 Reopen</button>
            <button class="task-act-btn task-act-green" onclick="updateTask('${t.id}', 'done')">✅ Move to Done</button>
          `;
        }

        return `
          <div class="task-card">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <span class="badge badge-purple" style="font-size:0.7rem;">${t.project_name || 'project'}</span>
              <div style="display:flex; gap:4px; align-items:center;">
                ${t.adapter ? `<span class="badge badge-blue" style="font-size:0.68rem; text-transform:uppercase;">${t.adapter}</span>` : ''}
                <span class="badge ${t.status === 'done' ? 'badge-green' : (t.status === 'active' ? 'badge-yellow' : (t.status === 'cancelled' ? 'badge-red' : ''))}" style="font-size:0.7rem;">${t.status}</span>
              </div>
            </div>
            <div class="task-summary">${t.summary || t.task_key || t.id}</div>
            <div class="task-meta" style="margin-top:6px;">
              <span>${t.runs && t.runs.length ? 'Run: ' + t.runs[0] : 'Pending'}${t.run_model ? ' (' + t.run_model + ')' : (t.model ? ' (' + t.model + ')' : '')}</span>
              <span style="color:var(--green); font-weight:500;">${t.run_speed ? t.run_speed + ' t/s' : ''}${t.run_usd_saved ? ' • +$' + t.run_usd_saved : ''}</span>
            </div>
            ${(t.adapter === 'agy' || (t.runs && t.runs[0] && t.runs[0].startsWith('agy-'))) ? `
              <div style="margin-top:8px; background:#161b22; border:1px solid #30363d; border-radius:6px; padding:6px 8px;">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
                  <span style="font-size:0.68rem; font-weight:600; color:var(--text-muted); text-transform:uppercase;">Connect Terminal</span>
                  ${t.conversation_id ? `
                    <button type="button" class="task-act-btn" style="background:#238636; color:#fff; border:none; padding:2px 8px; font-size:0.7rem; font-weight:600; border-radius:3px; cursor:pointer;" onclick="copyCmd('agy --conversation ${t.conversation_id}', this)">📋 Copy</button>
                  ` : ''}
                </div>
                ${t.conversation_id ? `
                  <input type="text" readonly value="agy --conversation ${t.conversation_id}" onclick="this.select()" title="Click to select all, then Ctrl+C" style="width:100%; background:#0d1117; border:1px solid #30363d; border-radius:4px; color:#58a6ff; font-family:monospace; font-size:0.75rem; padding:4px 6px; box-sizing:border-box; cursor:text; user-select:all;" />
                ` : ((t.run_status === 'failed' || t.status === 'cancelled' || t.run_error) ? `
                  <div style="font-size:0.75rem; color:var(--red); padding:4px 0; line-height:1.3; word-break:break-word;">
                    ❌ <strong>Launch Error:</strong> ${(t.run_error || 'Process failed during startup').substring(0, 180)}
                  </div>
                ` : `
                  <div style="font-size:0.72rem; color:var(--yellow); padding:3px 0;">⏳ Initializing session ID (starting up)...</div>
                `)}
              </div>
            ` : ''}
            ${t.run_quality !== null && t.run_quality !== undefined ? `<div style="margin-top:6px;"><span class="badge badge-green" style="font-size:0.72rem;">Quality: ${t.run_quality}/100</span></div>` : ''}
            ${t.note ? `<div style="margin-top:6px; font-size:0.72rem; color:var(--text-muted); font-style:italic;">"${t.note.substring(0, 100)}..."</div>` : ''}
            <div class="task-actions">
              ${actionBtns}
            </div>
          </div>
        `;
      };

      document.getElementById('tasksActive').innerHTML = activeTasks.slice(0, 15).map(renderTaskCard).join('') || '<div style="color:var(--text-muted);font-size:0.8rem;text-align:center;padding:20px 0;">No active tasks</div>';
      document.getElementById('tasksWaiting').innerHTML = waitingTasks.slice(0, 15).map(renderTaskCard).join('') || '<div style="color:var(--text-muted);font-size:0.8rem;text-align:center;padding:20px 0;">None</div>';
      document.getElementById('tasksDone').innerHTML = doneTasks.slice(0, 15).map(renderTaskCard).join('') || '<div style="color:var(--text-muted);font-size:0.8rem;text-align:center;padding:20px 0;">None</div>';
      document.getElementById('tasksCancelled').innerHTML = cancelledTasks.slice(0, 15).map(renderTaskCard).join('') || '<div style="color:var(--text-muted);font-size:0.8rem;text-align:center;padding:20px 0;">None</div>';
    }

    async function updateTask(taskId, newStatus) {
      try {
        const resp = await fetch('/api/task/update', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({task_id: taskId, status: newStatus})
        });
        const res = await resp.json();
        if (res.ok) {
          fetchData();
        } else {
          alert('Error updating task: ' + (res.error || 'unknown error'));
        }
      } catch (err) {
        alert('Network error: ' + err);
      }
    }

    async function cleanupStale(targetStatus) {
      const label = targetStatus === 'done' ? 'DONE' : 'TRASH / CANCELLED';
      if (!confirm(`Are you sure you want to move all old stale active/waiting tasks to ${label}?`)) {
        return;
      }
      try {
        const resp = await fetch('/api/tasks/cleanup-stale', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({max_age_hours: 2, target_status: targetStatus})
        });
        const res = await resp.json();
        if (res.ok) {
          alert(`Successfully cleaned up ${res.cleaned_count} task(s)!`);
          fetchData();
        } else {
          alert('Cleanup failed: ' + (res.error || 'unknown error'));
        }
      } catch (err) {
        alert('Network error: ' + err);
      }
    }

    async function fetchData() {
      try {
        const [overviewRes, runsRes, tasksRes] = await Promise.all([
          fetch('/api/overview').then(r => r.json()),
          fetch('/api/runs').then(r => r.json()),
          fetch('/api/tasks').then(r => r.json())
        ]);

        rawTasks = tasksRes;

        // Populate projects dropdown
        const projects = Array.from(new Set(rawTasks.map(t => t.project_name).filter(Boolean))).sort();
        const sel = document.getElementById('projectFilter');
        const curr = sel.value;
        sel.innerHTML = '<option value="all">All Projects (' + rawTasks.length + ')</option>' +
          projects.map(p => `<option value="${p}">${p}</option>`).join('');
        sel.value = curr || 'all';

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
              <td>${r.quality !== null && r.quality !== undefined ? '<span class="badge badge-green">' + r.quality + '</span>' : '-'}</td>
              <td>${r.evaluator ? '<span class="badge badge-purple">' + r.evaluator + '</span>' : '-'}</td>
              <td style="color: var(--green); font-weight: 500;">${r.usd_saved ? '+$' + r.usd_saved : '-'}</td>
            </tr>
          `).join('');
        }

        renderKanban();

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

        // Render Quotas
        rawQuotas = overviewRes.quotas || {};
        rawProjects = overviewRes.known_projects || [];

        if (rawQuotas) {
          const cq = rawQuotas.claude || {};
          const c5hRem = document.getElementById('claude5hRem');
          if (c5hRem) {
            c5hRem.innerText = cq.rem_5h !== undefined ? cq.rem_5h : 0;
            document.getElementById('claude5hLimit').innerText = cq.limit_5h !== undefined ? cq.limit_5h : 0;
            document.getElementById('claude5hUsed').innerText = cq.used_5h || 0;
            document.getElementById('claude5hReset').innerText = cq.reset_in || '--';
            document.getElementById('claude5hBar').style.width = (cq.pct_5h || 0) + '%';
            document.getElementById('claude5hBar').style.background = cq.pct_5h > 35 ? 'var(--green)' : 'var(--yellow)';

            document.getElementById('claude7dRem').innerText = cq.rem_7d !== undefined ? cq.rem_7d : 0;
            document.getElementById('claude7dLimit').innerText = cq.limit_7d !== undefined ? cq.limit_7d : 0;
            document.getElementById('claude7dUsed').innerText = cq.used_7d || 0;
            document.getElementById('claude7dBar').style.width = (cq.pct_7d || 0) + '%';
          }

          const aq = rawQuotas.agy || {};
          const a5hRem = document.getElementById('agy5hRem');
          if (a5hRem) {
            a5hRem.innerText = aq.rem_5h !== undefined ? aq.rem_5h : 0;
            document.getElementById('agy5hLimit').innerText = aq.limit_5h !== undefined ? aq.limit_5h : 0;
            document.getElementById('agy5hUsed').innerText = aq.used_5h || 0;
            document.getElementById('agy5hReset').innerText = aq.reset_in || '--';
            document.getElementById('agy5hBar').style.width = (aq.pct_5h || 0) + '%';
            document.getElementById('agy5hBar').style.background = aq.pct_5h > 35 ? 'var(--green)' : 'var(--yellow)';

            document.getElementById('agy7dRem').innerText = aq.rem_7d !== undefined ? aq.rem_7d : 0;
            document.getElementById('agy7dLimit').innerText = aq.limit_7d !== undefined ? aq.limit_7d : 0;
            document.getElementById('agy7dUsed').innerText = aq.used_7d || 0;
            document.getElementById('agy7dBar').style.width = (aq.pct_7d || 0) + '%';
          }

          const cx = rawQuotas.codex || {};
          const cx5hRem = document.getElementById('codex5hRem');
          if (cx5hRem) {
            cx5hRem.innerText = cx.rem_5h !== undefined ? cx.rem_5h : 0;
            document.getElementById('codex5hLimit').innerText = cx.limit_5h !== undefined ? cx.limit_5h : 0;
            document.getElementById('codex5hUsed').innerText = cx.used_5h || 0;
            document.getElementById('codex5hReset').innerText = cx.reset_in || '--';
            document.getElementById('codex5hBar').style.width = (cx.pct_5h || 0) + '%';
            document.getElementById('codex5hBar').style.background = cx.pct_5h > 35 ? 'var(--green)' : 'var(--yellow)';

            document.getElementById('codex7dRem').innerText = cx.rem_7d !== undefined ? cx.rem_7d : 0;
            document.getElementById('codex7dLimit').innerText = cx.limit_7d !== undefined ? cx.limit_7d : 0;
            document.getElementById('codex7dUsed').innerText = cx.used_7d || 0;
            document.getElementById('codex7dBar').style.width = (cx.pct_7d || 0) + '%';
          }

          const clPill = document.getElementById('claudeQuotaPill');
          if (clPill && cq.rem_5h !== undefined) {
            clPill.innerText = `Claude 5h: ${cq.rem_5h}/${cq.limit_5h} left (${cq.reset_in})`;
            clPill.className = 'badge ' + (cq.limit_5h > 0 ? (cq.pct_5h > 35 ? 'badge-green' : (cq.pct_5h > 15 ? 'badge-yellow' : 'badge-red')) : 'badge-purple');
          }

          updateModalQuotaBanner();
        }

      } catch (err) {
        console.error('Fetch error:', err);
      }
    }

    const ADAPTER_PRESETS = {
      local: [
        { label: 'Qwen 2.5 Coder 32B (Tier 0 Local vLLM default)', value: 'qwen2.5-coder-32b' },
        { label: 'Qwen 2.5 Coder 14B (Fast local worker)', value: 'qwen2.5-coder-14b' },
        { label: 'Local Fast Profile (No thinking overhead)', value: 'local-fast' }
      ],
      claude: [
        { label: 'Claude 3.7 Sonnet (Anthropic Tier 1 Architect)', value: 'claude-3-7-sonnet' },
        { label: 'Claude 3.5 Sonnet (Standard)', value: 'claude-3-5-sonnet' },
        { label: 'Claude 3.5 Haiku (Fast)', value: 'claude-3-5-haiku' },
        { label: 'Claude 3 Opus (Deep reasoning)', value: 'claude-3-opus' }
      ],
      agy: [
        { label: 'Gemini 3.8 Flash High (Default AGY)', value: 'gemini-3.8-flash-high' },
        { label: 'Gemini 2.5 Pro (Deep reasoning)', value: 'gemini-2.5-pro' },
        { label: 'Gemini 2.5 Flash (Ultra-fast)', value: 'gemini-2.5-flash' }
      ],
      codex: [
        { label: 'o3-mini (OpenAI reasoning)', value: 'o3-mini' },
        { label: 'GPT-4o (Standard)', value: 'gpt-4o' },
        { label: 'o1 (High capability)', value: 'o1' }
      ],
      architect: [
        { label: 'Gemini 2.5 Pro (Architect default)', value: 'gemini-2.5-pro' },
        { label: 'Gemini 3.8 Flash High', value: 'gemini-3.8-flash-high' },
        { label: 'Claude 3.7 Sonnet', value: 'claude-3-7-sonnet' }
      ]
    };

    function onAdapterChange() {
      const adapter = document.getElementById('spawnAdapter').value;
      const presets = ADAPTER_PRESETS[adapter] || [];
      const presetSel = document.getElementById('spawnModelPreset');
      presetSel.innerHTML = presets.map(p => `<option value="${p.value}">${p.label}</option>`).join('');
      if (presets.length > 0) {
        document.getElementById('spawnModel').value = presets[0].value;
      } else {
        document.getElementById('spawnModel').value = '';
      }
      updateModalQuotaBanner();
    }

    function onModelPresetChange() {
      const val = document.getElementById('spawnModelPreset').value;
      document.getElementById('spawnModel').value = val;
    }

    function updateModalQuotaBanner() {
      const adapter = document.getElementById('spawnAdapter').value;
      const q = (rawQuotas && rawQuotas[adapter]) || null;
      const titleEl = document.getElementById('bannerAdapterTitle');
      const resetEl = document.getElementById('bannerResetIn');
      const detailsEl = document.getElementById('bannerDetails');

      if (!titleEl) return;

      if (!q) {
        titleEl.innerText = adapter.toUpperCase() + ' Quota';
        resetEl.innerText = '--';
        detailsEl.innerText = 'Calculating rolling quotas...';
        return;
      }

      if (adapter === 'local') {
        titleEl.innerHTML = '🖥️ Local Cluster: <span style="color:var(--green);">∞ Unlimited Compute</span>';
        resetEl.innerText = 'Always Free';
        resetEl.className = 'badge badge-green';
        detailsEl.innerText = '0 API token cost • 4x RTX 3090 • Prefix cache active';
      } else {
        const isGreen = q.limit_5h > 0 ? (q.pct_5h > 35) : true;
        titleEl.innerHTML = `${q.label || adapter.toUpperCase()}: <span style="color:${isGreen ? 'var(--green)' : 'var(--yellow)'}; font-weight:600;">${q.rem_5h} / ${q.limit_5h} left (5h)</span>`;
        resetEl.innerText = 'Resets: ' + q.reset_in;
        resetEl.className = q.limit_5h > 0 ? (q.pct_5h > 35 ? 'badge badge-green' : (q.pct_5h > 15 ? 'badge badge-yellow' : 'badge badge-red')) : 'badge badge-purple';
        detailsEl.innerText = `Weekly: ${q.rem_7d} / ${q.limit_7d} remaining (${q.used_7d} used in 7 days)`;
      }
    }

    function onProjectSelectChange() {
      const sel = document.getElementById('spawnProjectSelect');
      const customInput = document.getElementById('spawnCwd');
      if (sel.value === '__custom__') {
        customInput.style.display = 'block';
        customInput.value = '';
        customInput.placeholder = '/path/to/custom/project';
        customInput.focus();
      } else {
        customInput.style.display = 'none';
        customInput.value = sel.value;
      }
    }

    function openSpawnModal() {
      const modal = document.getElementById('spawnModal');
      modal.style.display = 'flex';
      onAdapterChange();
      document.getElementById('spawnError').style.display = 'none';

      // Suggestions / projects dropdown
      const projSel = document.getElementById('spawnProjectSelect');
      const projects = rawProjects && rawProjects.length ? rawProjects : [{name: 'Current Directory', path: '.'}];
      projSel.innerHTML = projects.map(p => `<option value="${p.path}">${p.name} (${p.path})</option>`).join('') +
        '<option value="__custom__">📁 Custom Directory / New Project...</option>';
      projSel.value = projects[0].path;
      document.getElementById('spawnCwd').value = projects[0].path;
      document.getElementById('spawnCwd').style.display = 'none';

      updateModalQuotaBanner();
      setTimeout(() => document.getElementById('spawnTask').focus(), 50);
    }

    function closeSpawnModal() {
      document.getElementById('spawnModal').style.display = 'none';
      document.getElementById('spawnError').style.display = 'none';
    }

    async function submitSpawn() {
      const btn = document.getElementById('btnSpawnSubmit');
      const errDiv = document.getElementById('spawnError');
      errDiv.style.display = 'none';

      const adapter = document.getElementById('spawnAdapter').value;
      const model = document.getElementById('spawnModel').value.trim();
      const cwd = document.getElementById('spawnCwd').value.trim();
      const summary = document.getElementById('spawnSummary').value.trim();
      const task = document.getElementById('spawnTask').value.trim();

      if (!task) {
        errDiv.innerText = 'Please enter task prompt / instructions.';
        errDiv.style.display = 'block';
        return;
      }

      btn.disabled = true;
      btn.innerText = '⏳ Launching...';

      try {
        const resp = await fetch('/api/task/spawn', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({adapter, model, cwd, summary, task})
        });
        const res = await resp.json();
        if (res.ok) {
          closeSpawnModal();
          document.getElementById('spawnTask').value = '';
          document.getElementById('spawnSummary').value = '';
          fetchData();
          if (res.attach_command) {
            alert('🚀 Task dispatched successfully!\\n\\nTo connect / view session in terminal:\\n' + res.attach_command);
          }
        } else {
          errDiv.innerText = 'Launch failed: ' + (res.error || 'unknown error');
          errDiv.style.display = 'block';
        }
      } catch (err) {
        errDiv.innerText = 'Network error: ' + err;
        errDiv.style.display = 'block';
      } finally {
        btn.disabled = false;
        btn.innerText = '🚀 Launch Agent';
      }
    }

    // Auto-poll every 4 seconds
    fetchData();
    setInterval(fetchData, 4000);
  </script>

  <!-- Modal: Dispatch New Task -->
  <div id="spawnModal" class="modal-backdrop" style="display:none;" onclick="if(event.target===this)closeSpawnModal()">
    <div class="modal-box">
      <div class="modal-header">
        <h2>🚀 Dispatch New Task</h2>
        <button class="close-btn" onclick="closeSpawnModal()">&times;</button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label>Runner / Adapter</label>
          <select id="spawnAdapter" class="form-ctrl" onchange="onAdapterChange()">
            <optgroup label="Tier 0: Local GPU Cluster">
              <option value="local">🖥️ Local Worker (Claude Code + vLLM / Qwen on 4x RTX 3090)</option>
            </optgroup>
            <optgroup label="Tier 1: Architect Super-Models">
              <option value="claude">⚡ Claude Code (Anthropic Subscription / Sonnet 3.7)</option>
              <option value="agy">🧠 Google Antigravity (AGY / Gemini 2.5 Pro)</option>
              <option value="codex">🤖 OpenAI Codex (o3-mini / GPT-4o)</option>
              <option value="architect">🏛️ Gemini Architect Persona</option>
            </optgroup>
          </select>
        </div>

        <div class="form-group">
          <label>Target Model</label>
          <div style="display: flex; gap: 8px;">
            <select id="spawnModelPreset" class="form-ctrl" style="flex: 1;" onchange="onModelPresetChange()">
            </select>
            <input id="spawnModel" type="text" class="form-ctrl" style="flex: 1;" placeholder="Or type custom model..." />
          </div>
        </div>

        <!-- Live Quota Status Banner for selected model/adapter -->
        <div id="adapterQuotaBanner" style="background: rgba(88, 166, 255, 0.08); border: 1px solid rgba(88, 166, 255, 0.25); border-radius: 6px; padding: 10px 12px; font-size: 0.8rem;">
          <div style="display:flex; justify-content:space-between; margin-bottom:4px; align-items:center;">
            <span id="bannerAdapterTitle" style="font-weight:600; color:#fff;">--</span>
            <span id="bannerResetIn" class="badge badge-purple" style="font-size:0.7rem;">--</span>
          </div>
          <div id="bannerDetails" style="color:var(--text-muted); font-size:0.78rem;">--</div>
        </div>

        <div class="form-group">
          <label>Project</label>
          <select id="spawnProjectSelect" class="form-ctrl" onchange="onProjectSelectChange()">
          </select>
          <input id="spawnCwd" type="text" class="form-ctrl" style="margin-top: 6px; display: none;" placeholder="/absolute/path/to/custom/project" />
        </div>

        <div class="form-group">
          <label>Task Summary (Optional)</label>
          <input id="spawnSummary" type="text" class="form-ctrl" placeholder="Short label for Kanban card..." />
        </div>

        <div class="form-group">
          <label>Task Prompt / Instructions</label>
          <textarea id="spawnTask" class="form-ctrl" rows="6" placeholder="Provide clear, actionable instructions, files to modify, or tests to run..."></textarea>
        </div>

        <div id="spawnError" style="color: var(--red); font-size: 0.85rem; margin-top: 4px; display: none;"></div>
      </div>
      <div class="modal-footer">
        <button class="btn" style="background: #21262d; border: 1px solid var(--border);" onclick="closeSpawnModal()">Cancel</button>
        <button id="btnSpawnSubmit" class="btn" style="background: var(--green); color: white; font-weight: 600; border: none;" onclick="submitSpawn()">🚀 Launch Agent</button>
      </div>
    </div>
  </div>
</body>
</html>
"""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """HTTP Handler serving JSON API and Dashboard SPA."""

    def log_message(self, format, *args):
        # Silence default terminal noise during normal polling
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, data: Any, status: int = 200):
        try:
            body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_html(self, html: str, status: int = 200):
        try:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

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

        if path == "/api/quotas":
            self._send_json(compile_quotas())
            return

        if path == "/api/projects":
            self._send_json(get_known_projects())
            return

        self.send_error(404, f"Not Found: {path}")

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}

        if path == "/api/task/update":
            task_id = payload.get("task_id")
            status = payload.get("status")
            note = payload.get("note", "")
            if not task_id or not status:
                self._send_json({"ok": False, "error": "task_id and status required"}, status=400)
                return
            ok, err = update_task_status(task_id, status, note)
            if not ok:
                self._send_json({"ok": False, "error": err}, status=400)
                return
            self._send_json({"ok": True, "task_id": task_id, "new_status": status})
            return

        if path == "/api/tasks/cleanup-stale":
            max_age_hours = float(payload.get("max_age_hours", 2.0))
            target_status = payload.get("target_status", "done")
            count = cleanup_stale_tasks(max_age_hours=max_age_hours, target_status=target_status)
            self._send_json({"ok": True, "cleaned_count": count})
            return

        if path == "/api/task/spawn":
            adapter = payload.get("adapter", "local")
            task = payload.get("task", "")
            model = payload.get("model") or None
            cwd = payload.get("cwd") or None
            summary = payload.get("summary") or None
            ok, res = spawn_task_from_dashboard(adapter=adapter, task=task, model=model, cwd=cwd, summary=summary)
            if not ok:
                self._send_json({"ok": False, "error": res.get("error", "Failed to spawn task")}, status=400)
                return
            self._send_json({"ok": True, **res})
            return

        self.send_error(404, f"Not Found: {path}")


class ReusableHTTPServer(HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc_type, _, _ = sys.exc_info()
        if exc_type in (BrokenPipeError, ConnectionResetError):
            return
        super().handle_error(request, client_address)


def _find_pid_on_port(port: int) -> Optional[int]:
    try:
        res = subprocess.run(["fuser", f"{port}/tcp"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out = (res.stdout or res.stderr).strip()
        if out:
            pids = [int(p) for p in out.split() if p.isdigit()]
            if pids:
                return pids[0]
    except Exception:
        pass
    return None


def run_dashboard(host: str = "127.0.0.1", port: int = 8765, force: bool = False):
    try:
        server = ReusableHTTPServer((host, port), DashboardRequestHandler)
    except OSError as exc:
        if exc.errno == 98:  # Address already in use
            pid = _find_pid_on_port(port)
            if force and pid:
                print(f"[*] Port {port} occupied by PID {pid}. Terminating stale process...")
                try:
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.5)
                    server = ReusableHTTPServer((host, port), DashboardRequestHandler)
                except Exception as kerr:
                    print(f"[!] Could not terminate PID {pid}: {kerr}")
                    sys.exit(1)
            else:
                pid_hint = f" (PID {pid})" if pid else ""
                print(f"[!] Port {port} is already in use{pid_hint}.")
                print(f"    Run with --force to automatically kill the old instance:")
                print(f"    python3 dashboard.py --force")
                sys.exit(1)
        else:
            raise

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
    parser.add_argument("--force", "-f", action="store_true", help="Force kill any stale process holding the port")
    args = parser.parse_args()

    run_dashboard(host=args.host, port=args.port, force=args.force)

