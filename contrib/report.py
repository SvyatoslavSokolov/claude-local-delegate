#!/usr/bin/env python3
"""Analytics CLI closing the "analytics -> improve the delegation pipeline" loop.

Reads the local-delegate STATE DIR (metrics.jsonl, verified/, batches/,
coordination.sqlite3) via the shared metrics.py readers and emits:
  * a human terminal report (5 sections, also available as --md / --json), and
  * a FLAT per-task CSV (one row per delegation, pandas-loadable, no nested JSON).

Stdlib only. Never raises on missing data: missing state dir, empty ledger,
missing sqlite, missing verified/batches all degrade to "no data" lines.

Usage:
  python3 contrib/report.py [--days N] [--csv PATH] [--md PATH] [--json]
                            [--baseline D,C,T,K] [--state-dir PATH]
"""

import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import metrics  # noqa: E402  (shared readers, single source of truth)
import sqlite3
import time
from datetime import datetime, timezone

# Tunable thresholds (module-level constants, easy to adjust).
BLOCKED_PCT = 15        # flag a blocked category above this % of all runs
REVISE_PCT = 30         # flag verified revise rate above this %
BATCH_FAIL_PCT = 20     # flag share of zero-agent batches above this %
DELTA_PCT = 25          # flag |p50 vs baseline| delta above this %
LOW_WORTH = 50          # flag a complexity x profile cell below this worth_it
OVERRUN_RATIO = 1.5     # flag cells whose p50 actual/est ratio exceeds this

BASELINE_DEFAULT = "804,19,25000,67000"  # p50 duration_s, api_calls, output_tokens, peak_context

CSV_COLUMNS = [
    "run_id", "task_id", "task_key", "name", "project", "cwd",
    "created_at", "finished_at", "duration_s",
    "complexity", "est_minutes", "profile", "model", "read_only", "prompt_chars",
    "quality", "worth_it",
    "api_calls", "output_tokens", "input_tokens", "cached_input_tokens",
    "peak_context", "thinking_chars", "text_chars", "tool_calls",
    "blocked_category", "n_blocked",
    "verified", "verified_iterations", "batch_id",
    "task_status",
]


def default_state_dir():
    return os.environ.get(
        "CLAUDE_LOCAL_DELEGATE_STATE_DIR",
        os.path.expanduser("~/.claude-local-delegate"),
    )


# ---------------------------------------------------------------------------
# Load + join
# ---------------------------------------------------------------------------

def _safe_stats(rate):
    """Return the rate event's stats dict, or {} if missing/malformed."""
    if isinstance(rate, dict):
        stats = rate.get("stats")
        if isinstance(stats, dict):
            return stats
    return {}


def _run_rows(state_dir, days):
    """Join ledger + verified + batches + board into one dict per run.

    Returns a list of dicts, one per run (spawn events filtered by --days).
    Never raises: every join is best-effort.
    """
    runs = metrics.load_ledger(state_dir)
    verified = metrics.load_verified(state_dir)
    batches = metrics.load_batches(state_dir)
    task_status = _read_task_status(state_dir)

    now = time.time()
    rows = []
    for run_id, entry in runs.items():
        spawn = entry.get("spawn")
        if not isinstance(spawn, dict):
            spawn = {}
        rate = entry.get("rate")
        if not isinstance(rate, dict):
            rate = None
        blocked = entry.get("blocked") or []

        created_at = metrics.parse_timestamp(spawn.get("at"))
        if days is not None and days > 0:
            if created_at is None or created_at < now - days * 86400:
                continue

        row = {
            "run_id": run_id,
            "blocked_list": blocked,
            "task_id": spawn.get("task_id"),
            "task_key": spawn.get("task_key"),
            "name": spawn.get("name") or "",
            "project": spawn.get("cwd") or "",
            "cwd": spawn.get("cwd") or "",
            "created_at": created_at,
            "spawn": spawn,
            "rate": rate,
            "n_blocked": entry.get("n_blocked") or len(blocked),
            "blocked_category": (
                blocked[-1].get("category", "") if blocked and isinstance(blocked[-1], dict) else ""
            ),
        }

        # verified: this run is a current worker or appears in history
        row["verified"] = 0
        row["verified_iterations"] = ""
        for vid, v in verified.items():
            if not isinstance(v, dict):
                continue
            hist = v.get("history") or []
            hist_ids = {h.get("worker_id") for h in hist if isinstance(h, dict)}
            if v.get("current_worker_id") == run_id or run_id in hist_ids:
                row["verified"] = 1
                row["verified_iterations"] = v.get("iteration", "")
                break

        # batch: single batch whose agent_ids contain this run_id
        batch_id = ""
        for bid, b in batches.items():
            agents = b.get("agent_ids") or []
            if run_id in agents:
                batch_id = bid
                break
        row["batch_id"] = batch_id

        row["task_status"] = task_status.get(run_id, "")
        rows.append(row)
    return rows


def _read_task_status(state_dir):
    """Read coordination.sqlite3 tasks (read-only) -> {run_id: status}.

    Matches a run either via the task's JSON 'runs' list or its task_key.
    Missing db/table/any error -> empty dict, never raises.
    """
    out = {}
    path = os.path.join(state_dir, "coordination.sqlite3")
    if not os.path.exists(path):
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            cur = con.execute(
                "SELECT body FROM tasks"
            )
            for (body,) in cur:
                try:
                    task = json.loads(body)
                except (TypeError, ValueError):
                    continue
                if not isinstance(task, dict):
                    continue
                status = task.get("status", "")
                tkey = task.get("task_key")
                for r in task.get("runs") or []:
                    if isinstance(r, str):
                        out.setdefault(r, status)
                if isinstance(tkey, str) and tkey and tkey not in out:
                    out.setdefault(tkey, status)
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        pass
    return out


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def build_csv_rows(rows):
    """Map run rows to the flat CSV columns (no nesting, no lists)."""
    out = []
    for row in rows:
        rate = row["rate"]
        stats = _safe_stats(rate)
        spawn = row["spawn"]
        created = row["created_at"]
        finished = metrics.parse_timestamp(rate.get("at")) if rate else None
        duration = stats.get("duration_s") if stats else None
        blocked_cats = {
            e.get("category", "") for e in (row.get("blocked_list") or [])
            if isinstance(e, dict)
        }

        def _num(key, default=""):
            v = stats.get(key) if stats else None
            return v if v is not None else default

        out.append({
            "run_id": row["run_id"],
            "task_id": spawn.get("task_id") if spawn.get("task_id") is not None else "",
            "task_key": spawn.get("task_key") if spawn.get("task_key") is not None else "",
            "name": row["name"],
            "project": row["project"],
            "cwd": row["cwd"],
            "created_at": "" if created is None else created,
            "finished_at": "" if finished is None else finished,
            "duration_s": "" if duration is None else duration,
            "complexity": spawn.get("complexity", ""),
            "est_minutes": spawn.get("est_minutes", ""),
            "profile": spawn.get("profile", ""),
            "model": spawn.get("model", ""),
            "read_only": "" if spawn.get("read_only") is None else spawn.get("read_only"),
            "prompt_chars": "" if spawn.get("prompt_chars") is None else spawn.get("prompt_chars"),
            "quality": rate.get("quality", "") if rate else "",
            "worth_it": rate.get("worth_it", "") if rate else "",
            "api_calls": _num("api_calls"),
            "output_tokens": _num("output_tokens"),
            "input_tokens": _num("total_input_tokens"),
            "cached_input_tokens": _num("cached_input_tokens"),
            "peak_context": _num("peak_context"),
            "thinking_chars": _num("thinking_chars"),
            "text_chars": _num("text_chars"),
            "tool_calls": _num("tool_calls"),
            "blocked_category": row["blocked_category"],
            "n_blocked": row["n_blocked"],
            "verified": row["verified"],
            "verified_iterations": row["verified_iterations"],
            "batch_id": row["batch_id"],
            "task_status": row["task_status"],
        })
    return out


def write_csv(rows, path):
    """Write the flat CSV (header + one row per run, UTF-8)."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in build_csv_rows(rows):
            writer.writerow(row)


# ---------------------------------------------------------------------------
# Report computation
# ---------------------------------------------------------------------------

def _pct(part, whole):
    return (100.0 * part / whole) if whole else 0.0


def _iso(ts):
    if ts is None:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _week_start(ts):
    """ISO date (Monday-start) of the week containing ts."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    monday = datetime.fromordinal(dt.toordinal() - dt.weekday()).replace(tzinfo=timezone.utc)
    return monday.strftime("%Y-%m-%d")


def _rated(rows):
    return [r for r in rows if r["rate"] is not None]


def _mean(values):
    return (sum(values) / len(values)) if values else None


def compute_report(state_dir, days=30, baseline=None):
    """Compute the full report dict (cohort, trend, health, insights, lowest)."""
    if baseline is None:
        baseline = _parse_baseline(BASELINE_DEFAULT)
    rows = _run_rows(state_dir, days)
    rated = _rated(rows)

    # ---- 1) cohort
    total = len(rows)
    n_rated = len(rated)
    created = [r["created_at"] for r in rows if r["created_at"] is not None]
    cohort = {
        "total_runs": total,
        "rated_runs": n_rated,
        "rating_rate_pct": round(_pct(n_rated, total), 1),
        "span_start": _iso(min(created)) if created else "",
        "span_end": _iso(max(created)) if created else "",
        "blocked_runs": sum(1 for r in rows if r["n_blocked"] > 0),
    }

    # ---- 2) trend by ISO week (Monday start) over rated runs
    by_week = {}
    for r in rated:
        if r["created_at"] is None:
            continue
        week = _week_start(r["created_at"])
        by_week.setdefault(week, {"quality": [], "worth_it": []})
        by_week[week]["quality"].append(r["rate"].get("quality"))
        by_week[week]["worth_it"].append(r["rate"].get("worth_it"))
    trend = []
    for week in sorted(by_week):
        q = [v for v in by_week[week]["quality"] if v is not None]
        w = [v for v in by_week[week]["worth_it"] if v is not None]
        trend.append({
            "week": week,
            "n": len(by_week[week]["quality"]),
            "p50_quality": metrics.percentile(q, 50) if q else None,
            "p50_worth_it": metrics.percentile(w, 50) if w else None,
        })

    # ---- 3) health index
    health = {}

    # blocked by category (each blocked run counts once, under the category
    # of its last blocked event -- matches the CSV blocked_category column)
    cat_counts = {}
    for r in rows:
        c = r["blocked_category"]
        if c:
            cat_counts[c] = cat_counts.get(c, 0) + 1
    blocked_by_cat = {}
    for cat, n in sorted(cat_counts.items()):
        blocked_by_cat[cat] = {
            "count": n,
            "pct": round(_pct(n, total), 1),
            "flag": _pct(n, total) > BLOCKED_PCT,
        }
    health["blocked_by_category"] = blocked_by_cat

    # verified revise rate
    verified = metrics.load_verified(state_dir)
    v_cycles = [v for v in verified.values() if isinstance(v, dict)]
    n_rev = sum(
        1 for v in v_cycles
        if (v.get("iteration") or 0) >= 2 or len(v.get("history") or []) >= 1
    )
    revise_pct = round(_pct(n_rev, len(v_cycles)), 1)
    health["verified_revise_rate"] = {
        "cycles": len(v_cycles),
        "revised": n_rev,
        "pct": revise_pct,
        "flag": bool(v_cycles) and revise_pct > REVISE_PCT,
    }

    # fan-out batch success
    batches = metrics.load_batches(state_dir)
    n_batches = len(batches)
    n_empty = sum(1 for b in batches.values() if not b.get("agent_ids"))
    batch_fail_pct = round(_pct(n_empty, n_batches), 1)
    health["batch_fail"] = {
        "batches": n_batches,
        "empty": n_empty,
        "pct": batch_fail_pct,
        "flag": bool(n_batches) and batch_fail_pct > BATCH_FAIL_PCT,
    }

    # delta vs baseline (p50 over rated runs)
    def p50_of(stat_key):
        vals = []
        for r in rated:
            v = _safe_stats(r["rate"]).get(stat_key)
            if isinstance(v, (int, float)):
                vals.append(v)
        return metrics.percentile(vals, 50) if vals else None

    delta_metrics = {}
    for name, key, base in (
        ("duration_s", "duration_s", baseline[0]),
        ("api_calls", "api_calls", baseline[1]),
        ("output_tokens", "output_tokens", baseline[2]),
        ("peak_context", "peak_context", baseline[3]),
    ):
        value = p50_of(key)
        if value is None:
            delta_metrics[name] = {"value": None, "baseline": base, "pct": None, "flag": False}
        else:
            delta = 100.0 * (value - base) / base if base else 0.0
            delta_metrics[name] = {
                "value": value, "baseline": base,
                "pct": round(delta, 1), "flag": abs(delta) > DELTA_PCT,
            }
    health["delta_vs_baseline"] = delta_metrics

    # complexity x profile cells (rated runs)
    cells = {}
    for r in rated:
        spawn = r["spawn"]
        key = (spawn.get("complexity") or "?", spawn.get("profile") or "?")
        cell = cells.setdefault(key, {
            "n": 0, "est": [], "actual_min": [], "ratios": [],
            "quality": [], "worth_it": [],
        })
        cell["n"] += 1
        est = spawn.get("est_minutes")
        if isinstance(est, (int, float)) and est > 0:
            cell["est"].append(est)
        dur = _safe_stats(r["rate"]).get("duration_s")
        if isinstance(dur, (int, float)):
            actual_min = dur / 60.0
            cell["actual_min"].append(actual_min)
            if isinstance(est, (int, float)) and est > 0:
                cell["ratios"].append(actual_min / est)
        q = r["rate"].get("quality")
        if isinstance(q, (int, float)):
            cell["quality"].append(q)
        w = r["rate"].get("worth_it")
        if isinstance(w, (int, float)):
            cell["worth_it"].append(w)
    cell_rows = {}
    for (cx, prof), c in cells.items():
        p50_est = metrics.percentile(c["est"], 50) if c["est"] else None
        p50_act = metrics.percentile(c["actual_min"], 50) if c["actual_min"] else None
        p50_ratio = metrics.percentile(c["ratios"], 50) if c["ratios"] else None
        mean_w = _mean(c["worth_it"])
        mean_q = _mean(c["quality"])
        cell_rows["%s/%s" % (cx, prof)] = {
            "n": c["n"],
            "p50_est_minutes": p50_est,
            "p50_actual_minutes": None if p50_act is None else round(p50_act, 2),
            "p50_ratio": None if p50_ratio is None else round(p50_ratio, 2),
            "mean_quality": None if mean_q is None else round(mean_q, 1),
            "mean_worth_it": None if mean_w is None else round(mean_w, 1),
            "flag_low_worth": mean_w is not None and mean_w < LOW_WORTH,
            "flag_overrun": p50_ratio is not None and p50_ratio > OVERRUN_RATIO,
        }
    health["complexity_profile"] = cell_rows

    # ---- 4) insights (sorted by severity: blocked > revise > batch > delta > cells)
    insights = []
    for cat, info in sorted(blocked_by_cat.items()):
        if info["flag"]:
            insights.append((0,
                "blocked %s %.1f%% (threshold %d%%) -> add the missing tool / "
                "relax the corresponding gate (see DEFAULT_ALLOWED_TOOLS / "
                "permission mode)" % (cat, info["pct"], BLOCKED_PCT)))
    if health["verified_revise_rate"]["flag"]:
        insights.append((1,
            "verified revise-rate %.1f%% (threshold %d%%) -> tighten "
            "delegate_verified acceptance_criteria"
            % (health["verified_revise_rate"]["pct"], REVISE_PCT)))
    if health["batch_fail"]["flag"]:
        insights.append((2,
            "fan-out batch fail %.1f%% (threshold %d%%) -> narrow "
            "shared_instruction or split items"
            % (health["batch_fail"]["pct"], BATCH_FAIL_PCT)))
    for name, info in delta_metrics.items():
        if info["flag"] and info["pct"] is not None:
            insights.append((3,
                "p50 %s %s vs baseline %s (delta %.1f%%) -> check --tools grant / "
                "fixed overhead / model routing" % (name, info["value"], info["baseline"], info["pct"])))
    for label, cell in sorted(cell_rows.items()):
        if cell["flag_low_worth"]:
            insights.append((4,
                "%s mean worth_it %s (<%d) -> re-route this class to a better "
                "profile or split the task" % (label, cell["mean_worth_it"], LOW_WORTH)))
        if cell["flag_overrun"]:
            insights.append((5,
                "%s p50 est/actual ratio %s (>%s) -> est_minutes under-estimate; "
                "raise it or split" % (label, cell["p50_ratio"], OVERRUN_RATIO)))
    insights = [text for _, text in sorted(insights, key=lambda t: t[0])]

    # ---- 5) lowest worth_it (top 10 rated runs ascending)
    lowest = []
    for r in rated:
        w = r["rate"].get("worth_it")
        if not isinstance(w, (int, float)):
            continue
        stats = _safe_stats(r["rate"])
        note = r["rate"].get("note") or ""
        if len(note) > 60:
            note = note[:60]
        lowest.append({
            "run_id": r["run_id"],
            "name": r["name"],
            "complexity": r["spawn"].get("complexity", ""),
            "profile": r["spawn"].get("profile", ""),
            "minutes": None if not isinstance(stats.get("duration_s"), (int, float))
            else round(stats["duration_s"] / 60.0, 2),
            "output_tokens": stats.get("output_tokens", ""),
            "quality": r["rate"].get("quality", ""),
            "worth_it": w,
            "note": note,
        })
    lowest.sort(key=lambda x: x["worth_it"])
    lowest = lowest[:10]

    return {
        "cohort": cohort,
        "trend": trend,
        "health": health,
        "insights": insights,
        "lowest": lowest,
    }


def _parse_baseline(spec):
    try:
        parts = [float(x) for x in spec.split(",")]
        if len(parts) != 4:
            raise ValueError
        return parts
    except (ValueError, AttributeError):
        return [0.0, 0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# Terminal rendering
# ---------------------------------------------------------------------------

def render(report):
    """Render the report dict as the 5-section terminal/markdown text."""
    lines = []

    c = report["cohort"]
    lines.append("1) COHORT")
    lines.append("  total runs: %d, rated: %d (rating rate %.1f%%)"
                 % (c["total_runs"], c["rated_runs"], c["rating_rate_pct"]))
    span = "no data" if not c["span_start"] else "%s .. %s" % (c["span_start"], c["span_end"])
    lines.append("  date span: %s" % span)
    lines.append("  blocked runs: %d" % c["blocked_runs"])
    lines.append("")

    lines.append("2) TREND (ISO week, p50 quality / p50 worth_it)")
    if not report["trend"]:
        lines.append("  no rated runs")
    for t in report["trend"]:
        lines.append("  %s  n=%-3d  p50q=%s  p50w=%s"
                     % (t["week"], t["n"], t["p50_quality"], t["p50_worth_it"]))
    lines.append("")

    lines.append("3) HEALTH-INDEX")
    h = report["health"]
    bc = h["blocked_by_category"]
    if not bc:
        lines.append("  blocked by category: none")
    else:
        for cat, info in bc.items():
            flag = "  [FLAG >%d%%]" % BLOCKED_PCT if info["flag"] else ""
            lines.append("  blocked %-30s n=%-3d (%.1f%% of runs)%s"
                         % (cat, info["count"], info["pct"], flag))
    vr = h["verified_revise_rate"]
    if not vr["cycles"]:
        lines.append("  verified revise rate: no verified cycles")
    else:
        flag = "  [FLAG >%d%%]" % REVISE_PCT if vr["flag"] else ""
        lines.append("  verified revise rate: %d/%d = %.1f%%%s"
                     % (vr["revised"], vr["cycles"], vr["pct"], flag))
    bf = h["batch_fail"]
    if not bf["batches"]:
        lines.append("  fan-out batch success: no batches")
    else:
        flag = "  [FLAG >%d%% empty]" % BATCH_FAIL_PCT if bf["flag"] else ""
        lines.append("  fan-out batch success: %d empty of %d = %.1f%%%s"
                     % (bf["empty"], bf["batches"], bf["pct"], flag))
    lines.append("  delta vs baseline (p50 rated runs):")
    for name, info in h["delta_vs_baseline"].items():
        if info["value"] is None:
            lines.append("    %-16s no data (baseline %s)" % (name, info["baseline"]))
        else:
            flag = "  [FLAG |delta|>%d%%]" % DELTA_PCT if info["flag"] else ""
            lines.append("    %-16s %s vs %s  delta %+.1f%%%s"
                         % (name, info["value"], info["baseline"], info["pct"], flag))
    cells = h["complexity_profile"]
    if not cells:
        lines.append("  complexity x profile: no rated runs")
    else:
        for label, cell in sorted(cells.items()):
            flags = []
            if cell["flag_low_worth"]:
                flags.append("LOW_WORTH<%d" % LOW_WORTH)
            if cell["flag_overrun"]:
                flags.append("OVERRUN>%s" % OVERRUN_RATIO)
            fl = "  [%s]" % " | ".join(flags) if flags else ""
            lines.append("  cx %s: n=%d p50est=%s p50act=%s ratio=%s q=%s w=%s%s"
                         % (label, cell["n"], cell["p50_est_minutes"],
                            cell["p50_actual_minutes"], cell["p50_ratio"],
                            cell["mean_quality"], cell["mean_worth_it"], fl))
    lines.append("")

    lines.append("4) INSIGHTS (problem -> named action)")
    if not report["insights"]:
        lines.append("  no insights: all metrics within thresholds")
    for text in report["insights"]:
        lines.append("  - %s" % text)
    lines.append("")

    lines.append("5) LOWEST worth_it (top 10)")
    if not report["lowest"]:
        lines.append("  no rated runs")
    for r in report["lowest"]:
        lines.append("  %s  %-25s cx=%-7s prof=%-6s min=%s out=%s q=%s w=%s  %s"
                     % (r["run_id"], (r["name"] or "")[:25], r["complexity"],
                        r["profile"], r["minutes"], r["output_tokens"],
                        r["quality"], r["worth_it"], r["note"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Delegation pipeline analytics report")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--csv", default=None, dest="csv_path")
    parser.add_argument("--md", default=None)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--baseline", default=BASELINE_DEFAULT)
    args = parser.parse_args(argv)

    state_dir = args.state_dir or default_state_dir()
    report = compute_report(state_dir, days=args.days, baseline=_parse_baseline(args.baseline))

    if args.as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    rows = _run_rows(state_dir, args.days)
    if args.csv_path:
        try:
            write_csv(rows, args.csv_path)
        except OSError as exc:
            print("warning: could not write csv: %s" % exc, file=sys.stderr)

    text = render(report)
    if args.md:
        try:
            with open(args.md, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError as exc:
            print("warning: could not write md: %s" % exc, file=sys.stderr)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())