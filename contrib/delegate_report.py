#!/usr/bin/env python3
"""Report over the local-delegate metrics ledger (~/.claude-local-delegate/metrics.jsonl).

Joins "spawn" and "rate" events by run_id and prints:
  1) per complexity x profile aggregates,
  2) the lowest worth_it runs,
  3) --schedule: list-scheduling ETA over pending runs plus any --plan tasks.

Stdlib only.
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

DEFAULT_LEDGER = os.path.expanduser(
    "~/.claude-local-delegate/metrics.jsonl")
FALLBACK_MINUTES = 15


def p50(values):
    """Nearest-rank p50; None for empty input."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, int(math.ceil(0.5 * len(ordered)))) - 1]


def mean(values):
    return round(sum(values) / len(values), 2) if values else None


def actual_minutes(stats):
    if isinstance(stats, dict):
        v = stats.get("duration_s")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v / 60.0
    return None


def load_ledger(path):
    """Return {run_id: {"spawn":..., "rate":...}}; later rate wins.

    Tolerates blank/garbage lines.  Returns None if the file does not exist,
    an empty dict if it exists but is empty/unparseable.
    """
    if not os.path.exists(path):
        return None
    runs = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            run_id = event.get("run_id")
            if not run_id:
                continue
            kind = event.get("event")
            entry = runs.setdefault(run_id, {})
            if kind == "spawn":
                entry["spawn"] = event
            elif kind == "rate":
                entry["rate"] = event  # later rate wins
    return runs


def complexity_key(complexity):
    return complexity if complexity else "unknown"


def table_rows(runs):
    """Section 1: per complexity x profile aggregates."""
    cells = defaultdict(list)
    for entry in runs.values():
        spawn = entry.get("spawn")
        if not isinstance(spawn, dict):
            continue
        key = (complexity_key(spawn.get("complexity")),
               spawn.get("profile") or "unknown")
        rated = entry.get("rate")
        stats = rated.get("stats") if isinstance(rated, dict) else None
        cells[key].append({
            "spawn": spawn,
            "rated": isinstance(rated, dict),
            "est": spawn.get("est_minutes"),
            "actual": actual_minutes(stats),
            "output": (stats or {}).get("output_tokens"),
            "thinking": (stats or {}).get("thinking_chars"),
            "text": (stats or {}).get("text_chars"),
            "quality": (rated or {}).get("quality"),
            "worth_it": (rated or {}).get("worth_it"),
        })

    rows = []
    for (complexity, profile) in sorted(cells, key=lambda k: (k[0], k[1])):
        items = cells[(complexity, profile)]
        ratios = [r["actual"] / r["est"] for r in items
                  if isinstance(r["est"], (int, float)) and not isinstance(r["est"], bool)
                  and r["est"] > 0 and r["actual"] is not None]
        thinking_shares = []
        for r in items:
            if isinstance(r["thinking"], (int, float)) and isinstance(r["text"], (int, float)):
                denom = r["thinking"] + r["text"]
                if denom > 0:
                    thinking_shares.append(r["thinking"] / denom)
        rows.append({
            "complexity": complexity,
            "profile": profile,
            "n": len(items),
            "rated": sum(1 for r in items if r["rated"]),
            "p50_est_minutes": p50([r["est"] for r in items
                                    if isinstance(r["est"], (int, float)) and not isinstance(r["est"], bool)]),
            "p50_actual_minutes": p50([r["actual"] for r in items
                                       if r["actual"] is not None]),
            "p50_ratio": p50(ratios),
            "p50_output_tokens": p50([r["output"] for r in items
                                      if isinstance(r["output"], (int, float)) and not isinstance(r["output"], bool)]),
            "p50_thinking_share": p50(thinking_shares),
            "mean_quality": mean([r["quality"] for r in items
                                  if isinstance(r["quality"], (int, float))]),
            "mean_worth_it": mean([r["worth_it"] for r in items
                                   if isinstance(r["worth_it"], (int, float))]),
        })
    return rows


def lowest_worth(runs, limit=10):
    """Section 2: worst rated runs."""
    rows = []
    for run_id, entry in runs.items():
        rated = entry.get("rate")
        if not isinstance(rated, dict):
            continue
        spawn = entry.get("spawn") or {}
        stats = rated.get("stats")
        if not isinstance(stats, dict):
            stats = {}
        rows.append({
            "run_id": run_id,
            "name": spawn.get("name"),
            "complexity": complexity_key(spawn.get("complexity")),
            "profile": spawn.get("profile") or "unknown",
            "minutes": round(actual_minutes(stats), 2) if stats.get("duration_s") is not None else None,
            "output_tokens": stats.get("output_tokens"),
            "quality": rated.get("quality"),
            "worth_it": rated.get("worth_it"),
            "note": rated.get("note"),
        })
    rows.sort(key=lambda r: (r["worth_it"] is None, r["worth_it"], r["run_id"]))
    return rows[:limit]


# ---------------------------------------------------------------------------
# --schedule: list scheduling
# ---------------------------------------------------------------------------

def estimate_minutes(spawn, p50_actual_by_complexity):
    est = spawn.get("est_minutes")
    if isinstance(est, (int, float)) and not isinstance(est, bool) and est > 0:
        return est
    c = spawn.get("complexity")
    v = p50_actual_by_complexity.get(complexity_key(c))
    return v if v is not None else FALLBACK_MINUTES


def find_cycle(preds):
    """Return a list of keys forming a cycle among the pred edges, or None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in preds}
    stack = []

    def dfs(u):
        color[u] = GRAY
        stack.append(u)
        for v in preds[u]:
            if color[v] == GRAY:
                # stack[i:] is the cycle; the edge v->...->v closes it
                return list(stack[stack.index(v):])
            if color[v] == WHITE:
                cyc = dfs(v)
                if cyc:
                    return cyc
        stack.pop()
        color[u] = BLACK
        return None

    for k in list(preds):
        if color[k] == WHITE:
            cyc = dfs(k)
            if cyc:
                return cyc
    return None


def list_schedule(tasks, preds, pool):
    """Greedy list scheduling: longest-est first among ready tasks.

    tasks: {key: est_minutes}; preds: {key: (set of keys that must finish first)}
    A task starts at max(earliest free slot, latest predecessor finish) on the
    slot that minimizes that.  Returns {"schedules": {key: (start, finish)},
    "makespan": float, "critical_path": [keys]}, or None if not all tasks
    could be scheduled (cycle).
    """
    est = {k: float(v) for k, v in tasks.items()}
    # Work on a copy: the loop below drains sets, and the critical-path pass
    # needs the original edge sets.
    remaining = {k: set(s) for k, s in preds.items()}
    ready = [k for k in remaining if not remaining[k]]
    priority = lambda k: (-est[k], k)  # longest est first, key as tie-break
    ready.sort(key=priority)
    slot_times = [0.0] * max(1, pool)
    schedules = {}
    while ready:
        k = ready.pop(0)
        # start bounded by predecessors' finish times, not just slot freedom
        pred_finish = max((schedules[p][1] for p in preds[k] if p in schedules),
                          default=0.0)
        best_i, best_t = None, None
        for i, free in enumerate(slot_times):
            t = max(free, pred_finish)
            if best_t is None or t < best_t or (t == best_t and i < best_i):
                best_i, best_t = i, t
        finish = best_t + est[k]
        slot_times[best_i] = finish
        schedules[k] = (best_t, finish)
        for m in list(remaining):
            if m in schedules or m in ready:
                continue
            remaining[m].discard(k)
            if not remaining[m]:
                ready.append(m)
                ready.sort(key=priority)
    if len(schedules) < len(tasks):
        return None
    makespan = max(f for _s, f in schedules.values())
    # Critical path: longest est-sum path in the DAG (ties: lexicographic).
    crit = {}

    def cp(u):
        if u in crit:
            return crit[u]
        if not preds[u]:
            crit[u] = (est[u], [u])
        else:
            best = None
            for v in sorted(preds[u]):
                val = cp(v)
                if best is None or val[0] > best[0] or (val[0] == best[0] and val[1] < best[1]):
                    best = val
            crit[u] = (best[0] + est[u], best[1] + [u])
        return crit[u]

    best = None
    for k in sorted(tasks):
        val = cp(k)
        if best is None or val[0] > best[0] or (val[0] == best[0] and val[1] < best[1]):
            best = val
    return {"schedules": schedules, "makespan": makespan,
            "critical_path": best[1] if best else []}


def p50_actual_by_complexity(runs):
    buckets = defaultdict(list)
    for entry in runs.values():
        rated = entry.get("rate")
        if not isinstance(rated, dict):
            continue
        spawn = entry.get("spawn") or {}
        a = actual_minutes(rated.get("stats"))
        if a is None:
            continue
        buckets[complexity_key(spawn.get("complexity"))].append(a)
    return {k: p50(v) for k, v in buckets.items()}


def build_schedule(runs, plan, pool):
    """Merge pending runs + plan tasks; return dict for printing or a
    {"cycle": [...]} error dict."""
    p50_by_cx = p50_actual_by_complexity(runs)
    tasks = {}
    preds = {}
    labels = {}

    # "blocks" lists the successors this task holds up: X.blocks=[Y] means Y
    # cannot start before X finishes, so X becomes a predecessor of Y.
    def add(key, est, blocks, label):
        tasks[key] = est
        preds.setdefault(key, set())
        for succ in blocks:
            preds.setdefault(succ, set()).add(key)
        labels[key] = label

    rated_ids = {rid for rid, e in runs.items() if isinstance(e.get("rate"), dict)}
    for run_id, entry in sorted(runs.items()):
        if run_id in rated_ids:
            continue
        spawn = entry.get("spawn")
        if not isinstance(spawn, dict):
            continue
        blocks = spawn.get("blocks") or []
        key = spawn.get("task_key") or spawn.get("name") or run_id
        add(key, estimate_minutes(spawn, p50_by_cx),
            set(str(b) for b in blocks), spawn.get("name") or run_id)
    for item in plan or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key"))
        if not key:
            continue
        c = item.get("complexity")
        est = item.get("est_minutes")
        if not (isinstance(est, (int, float)) and not isinstance(est, bool) and est > 0):
            v = p50_by_cx.get(complexity_key(c))
            est = v if v is not None else FALLBACK_MINUTES
        blocks = item.get("blocks") or []
        add(key, est, set(str(b) for b in blocks),
            item.get("name") or key)

    # Restrict to known keys (plan may reference runs; missing refs drop out).
    known = set(tasks)
    preds = {k: {p for p in preds.get(k, ()) if p in known} for k in known}
    return tasks, preds, labels


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Aggregate report over the local-delegate metrics ledger.")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER,
                        help="Path to metrics.jsonl (default: %(default)s)")
    parser.add_argument("--json", action="store_true",
                        help="Machine-readable JSON output.")
    parser.add_argument("--schedule", action="store_true",
                        help="List-scheduling ETA for pending runs plus --plan.")
    parser.add_argument("--pool", type=int, default=3,
                        help="Pool size for --schedule (default: %(default)s)")
    parser.add_argument("--plan", metavar="FILE",
                        help="JSON list of {key, est_minutes, blocks} tasks to schedule.")
    args = parser.parse_args(argv)

    runs = load_ledger(args.ledger)
    if runs is None:
        print("no ledger yet at %s -- nothing to report." % args.ledger)
        return 0

    plan = None
    if args.plan:
        with open(args.plan, "r", encoding="utf-8") as fh:
            plan = json.load(fh)
        if not isinstance(plan, list):
            print("error: --plan file must contain a JSON list", file=sys.stderr)
            return 1

    if args.schedule:
        tasks, preds, labels = build_schedule(runs, plan, args.pool)
        if not tasks:
            print("schedule: nothing pending (every spawned run is rated) and no --plan given.")
            return 0
        cyc = find_cycle(preds)
        sched = list_schedule(tasks, preds, args.pool)
        if cyc or sched is None:
            print("schedule error: cycle detected: " + " -> ".join(map(str, cyc)))
            return 1
        result = {
            "pool": args.pool,
            "tasks": [
                {"key": k, "name": labels[k], "est_minutes": tasks[k],
                 "start_min": round(sched["schedules"][k][0], 3),
                 "finish_min": round(sched["schedules"][k][1], 3)}
                for k in sorted(tasks, key=lambda k: (sched["schedules"][k][0], k))
            ],
            "makespan_min": round(sched["makespan"], 3),
            "critical_path": sched["critical_path"],
        }
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=False))
        else:
            if not tasks:
                print("no pending runs and no --plan tasks; nothing to schedule.")
                return 0
            hdr = "%-28s %-24s %8s %10s %10s" % (
                "key", "name", "est", "start", "finish")
            print(hdr)
            print("-" * len(hdr))
            for t in result["tasks"]:
                print("%-28s %-24s %8s %10.1f %10.1f" % (
                    t["key"], (t["name"] or "")[:24], t["est_minutes"],
                    t["start_min"], t["finish_min"]))
            print("makespan: %.1f min" % result["makespan_min"])
            print("critical path: " + " -> ".join(result["critical_path"]))
        return 0

    report = {
        "ledger": args.ledger,
        "runs": len(runs),
        "rated": sum(1 for e in runs.values() if isinstance(e.get("rate"), dict)),
        "by_complexity_profile": table_rows(runs),
        "lowest_worth": lowest_worth(runs),
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        print("ledger: %s  runs: %d  rated: %d" % (
            args.ledger, report["runs"], report["rated"]))
        hdr = "%-10s %-8s %4s %5s %6s %8s %7s %10s %9s %7s %7s" % (
            "complexity", "profile", "n", "rated", "p50est", "p50act",
            "p50ratio", "p50out", "p50think", "meanQ", "meanW")
        print()
        print(hdr)
        print("-" * len(hdr))
        for r in report["by_complexity_profile"]:
            print("%-10s %-8s %4d %5d %6s %8s %7s %10s %9s %7s %7s" % (
                r["complexity"], r["profile"], r["n"], r["rated"],
                _f(r["p50_est_minutes"]), _f(r["p50_actual_minutes"]),
                _f(r["p50_ratio"]), _f(r["p50_output_tokens"]),
                _f(r["p50_thinking_share"], 3), _f(r["mean_quality"]),
                _f(r["mean_worth_it"])))
        print()
        print("lowest worth_it:")
        lhdr = "%-10s %-24s %-10s %-8s %7s %9s %7s %8s  %s" % (
            "run_id", "name", "cx", "profile", "min", "out", "quality",
            "worth", "note")
        print(lhdr)
        print("-" * len(lhdr))
        for r in report["lowest_worth"]:
            print("%-10s %-24s %-10s %-8s %7s %9s %7s %8s  %s" % (
                r["run_id"], (r["name"] or "")[:24], r["complexity"],
                r["profile"], _f(r["minutes"], 2), _f(r["output_tokens"]),
                _f(r["quality"]), _f(r["worth_it"]), r["note"] or ""))
    return 0


def _f(v, nd=1):
    if v is None:
        return "-"
    return ("%%.%df" % nd) % v


if __name__ == "__main__":
    sys.exit(main())