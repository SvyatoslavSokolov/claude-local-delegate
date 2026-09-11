#!/usr/bin/env python3
"""Compact JSON aggregate metrics over the historical local-delegate cohort.

The cohort is defined exactly by the keys of ``session-starts.json`` (the known
local-delegate session IDs mapped to their Unix start seconds). For each cohort
ID we locate ``<session-id>.jsonl`` recursively under ``<claude-dir>/projects``
and stream it once, retaining only aggregate numbers -- never the prompt or
final text, paths, session IDs, environment, URLs or keys.

Stdlib only. See COORDINATION.md / TASK_DESIGN.md for context.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone

# datetime.fromisoformat (Python 3.8) only accepts 0, 3 or 6 fractional-second
# digits. Transcript timestamps vary in precision, so normalize any fractional
# group to exactly 6 digits before parsing.
_FRAC_RE = re.compile(r"\.(\d+)")


def _pad_frac(match):
    digits = (match.group(1) + "000000")[:6]
    return "." + digits

SCHEMA_VERSION = 2
PEAK_THRESHOLDS = (32768, 65536, 131072, 262144)
DURATION_THRESHOLDS = (120, 300, 600)
PERCENTILES = (50, 75, 90, 95)
TOP_TOOLS = 12


def parse_timestamp(value):
    """Return Unix seconds for an ISO string or numeric value, else None."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        # Normalize the fractional-seconds group to 6 digits for fromisoformat.
        s = _FRAC_RE.sub(_pad_frac, s)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def nearest_rank(values, percentile):
    """Deterministic nearest-rank percentile. Returns None for empty input."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    rank = int(math.ceil(percentile / 100.0 * n))
    rank = max(1, min(n, rank))
    return ordered[rank - 1]


def percentile_block(values):
    block = {"mean": round(sum(values) / len(values), 2) if values else None}
    for p in PERCENTILES:
        block["p%d" % p] = nearest_rank(values, p)
    block["max"] = max(values) if values else None
    return block


def stream_transcript(path, start_ts):
    """Stream one transcript and return its aggregate metrics (no text kept).

    Raises OSError/IOError if the file cannot be opened; the caller retries the
    next hardlinked copy or records it as unreadable.
    """
    # Claude Code splits one API response across several "assistant" entries
    # that share message.id and each repeat the same usage.  Keep only the
    # last usage per id so tokens and "turns" (unique API responses) are not
    # inflated; entries without an id count individually.
    usage_by_id = {}
    anon_turns = 0
    sum_input = sum_cread = sum_ccreate = sum_output = 0
    peak = 0
    final_chars = 0
    final_lines = 0
    tool_calls = 0
    tools = Counter()
    max_ts = None

    def add_usage(usage):
        nonlocal sum_input, sum_cread, sum_ccreate, sum_output, peak
        inp = usage.get("input_tokens") or 0
        cr = usage.get("cache_read_input_tokens") or 0
        cc = usage.get("cache_creation_input_tokens") or 0
        out = usage.get("output_tokens") or 0
        sum_input += inp
        sum_cread += cr
        sum_ccreate += cc
        sum_output += out
        pc = inp + cr + cc
        if pc > peak:
            peak = pc

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
            ts = parse_timestamp(event.get("timestamp"))
            if ts is not None and (max_ts is None or ts > max_ts):
                max_ts = ts
            if event.get("type") != "assistant":
                continue
            message = event.get("message")
            if not isinstance(message, dict):
                message = {}
            usage = message.get("usage")
            if not isinstance(usage, dict):
                usage = {}
            msg_id = message.get("id")
            if msg_id:
                usage_by_id[msg_id] = usage  # later entry repeats same usage
            else:
                anon_turns += 1
                add_usage(usage)
            content = message.get("content")
            if isinstance(content, list):
                texts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        txt = block.get("text")
                        if txt:
                            texts.append(txt)
                    elif btype == "tool_use":
                        tool_calls += 1
                        tools[block.get("name") or "unknown"] += 1
                if texts:
                    joined = "".join(texts)
                    final_chars = len(joined)
                    final_lines = joined.count("\n") + 1

    for usage in usage_by_id.values():
        add_usage(usage)
    assistant_turns = len(usage_by_id) + anon_turns

    if max_ts is None:
        max_ts = float(os.path.getmtime(path))
    duration = max_ts - start_ts
    if duration < 0:
        duration = 0.0

    return {
        "duration_seconds": round(duration, 2),
        "turns": assistant_turns,
        "input_tokens": sum_input,
        "cache_read_input_tokens": sum_cread,
        "cache_creation_input_tokens": sum_ccreate,
        "output_tokens": sum_output,
        "total_input_tokens": sum_input + sum_cread + sum_ccreate,
        "peak_context_tokens": peak,
        "final_chars": final_chars,
        "final_lines": final_lines,
        "tool_calls": tool_calls,
        "tools": tools,
    }


def load_roster_states():
    """Return {sessionId: state} from the roster, or None if unavailable."""
    # 1) the cheap file.
    try:
        with open("/tmp/all_agents.json", "r", encoding="utf-8") as fh:
            roster = json.load(fh)
        if isinstance(roster, list):
            out = {}
            for entry in roster:
                if not isinstance(entry, dict):
                    continue
                sid = entry.get("sessionId")
                if sid:
                    out[sid] = entry.get("state")
            if out:
                return out
    except (OSError, ValueError):
        pass
    # 2) fall back to the CLI.
    try:
        p = subprocess.run(
            ["claude", "agents", "--all", "--json"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
        )
        raw = p.stdout.decode("utf-8", "replace")
        start = raw.find("[")
        roster = json.loads(raw[start:]) if start >= 0 else []
        out = {}
        for entry in roster:
            if not isinstance(entry, dict):
                continue
            sid = entry.get("sessionId")
            if sid:
                out[sid] = entry.get("state")
        return out or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def build_index(projects_dir, cohort):
    """Map each cohort ID to the list of transcript paths found for it."""
    index = {}
    if not os.path.isdir(projects_dir):
        return index
    for root, _dirs, files in os.walk(projects_dir):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            sid = name[:-6]
            if sid in cohort and len(index.get(sid, ())) < 3:
                index.setdefault(sid, []).append(os.path.join(root, name))
    return index


def summarize(metric_list, tool_counter):
    n = len(metric_list)
    durations = [m["duration_seconds"] for m in metric_list]
    turns = [m["turns"] for m in metric_list]
    total_input = [m["total_input_tokens"] for m in metric_list]
    peak = [m["peak_context_tokens"] for m in metric_list]
    output = [m["output_tokens"] for m in metric_list]
    final_chars = [m["final_chars"] for m in metric_list]
    final_lines = [m["final_lines"] for m in metric_list]
    tool_calls = [m["tool_calls"] for m in metric_list]

    def threshold_counts(values, thresholds):
        result = {}
        for t in thresholds:
            c = sum(1 for v in values if v > t)
            pct = round(100.0 * c / n, 2) if n else 0.0
            result["gt_%d" % t] = {"count": c, "percent": pct}
        return result

    sums = {
        "input_tokens": sum(m["input_tokens"] for m in metric_list),
        "cache_read_input_tokens": sum(m["cache_read_input_tokens"] for m in metric_list),
        "cache_creation_input_tokens": sum(
            m["cache_creation_input_tokens"] for m in metric_list),
        "output_tokens": sum(m["output_tokens"] for m in metric_list),
        "total_input_tokens": sum(m["total_input_tokens"] for m in metric_list),
    }

    top_tools = [
        {"name": name, "count": count}
        for name, count in tool_counter.most_common(TOP_TOOLS)
    ]

    return {
        "percentiles": {
            "duration_seconds": percentile_block(durations),
            "turns": percentile_block(turns),
            "total_input_tokens": percentile_block(total_input),
            "peak_context_tokens": percentile_block(peak),
            "output_tokens": percentile_block(output),
            "final_chars": percentile_block(final_chars),
            "final_lines": percentile_block(final_lines),
            "tool_calls": percentile_block(tool_calls),
        },
        "sums": sums,
        "top_tools": top_tools,
        "thresholds": {
            "peak_context_tokens": threshold_counts(peak, PEAK_THRESHOLDS),
            "duration_seconds": threshold_counts(durations, DURATION_THRESHOLDS),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Emit compact JSON aggregate metrics for the historical "
                    "local-delegate cohort.")
    parser.add_argument("--state-dir", default=os.path.expanduser(
        "~/.claude-local-delegate"),
        help="State directory holding session-starts.json (default: %(default)s)")
    parser.add_argument("--claude-dir", default=os.path.expanduser("~/.claude"),
        help="Claude home holding projects/ (default: %(default)s)")
    parser.add_argument("--pretty", action="store_true",
                        help="Indent the JSON output (default: compact).")
    args = parser.parse_args(argv)

    starts_path = os.path.join(args.state_dir, "session-starts.json")
    try:
        with open(starts_path, "r", encoding="utf-8") as fh:
            starts = json.load(fh)
    except (OSError, ValueError) as exc:
        print("error: cannot read session-starts.json at %s: %s" % (
            starts_path, exc), file=sys.stderr)
        return 1
    if not isinstance(starts, dict):
        print("error: session-starts.json is not an object", file=sys.stderr)
        return 1

    cohort = list(starts.keys())
    cohort_set = set(cohort)

    projects_dir = os.path.join(args.claude_dir, "projects")
    index = build_index(projects_dir, cohort_set)

    metric_list = []
    tool_counter = Counter()
    matched = 0
    unreadable = 0
    for sid in cohort:
        if sid not in index:
            continue
        matched += 1
        start_ts = starts[sid]
        got = False
        seen_inodes = set()
        for path in index[sid]:
            try:
                ino = os.stat(path).st_ino
            except OSError:
                continue
            if ino in seen_inodes:
                continue
            seen_inodes.add(ino)
            try:
                m = stream_transcript(path, start_ts)
            except OSError:
                continue
            metric_list.append(m)
            tool_counter.update(m["tools"])
            got = True
            break
        if not got:
            unreadable += 1

    missing = len(cohort) - matched

    roster = load_roster_states()
    if roster is not None:
        rostered = [sid for sid in cohort if sid in roster]
        state_counts = Counter(roster[sid] for sid in rostered)
        completed = state_counts.get("done", 0)
        completed_pct = round(100.0 * completed / len(rostered), 2) if rostered else 0.0
        completed_state = {
            "available": True,
            "in_roster": len(rostered),
            "state_counts": dict(state_counts),
            "completed_count": completed,
            "completed_percent": completed_pct,
        }
    else:
        completed_state = {"available": False, "in_roster": 0,
                           "state_counts": {}, "completed_count": 0,
                           "completed_percent": None}

    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cohort": len(cohort),
        "matched": matched,
        "missing": missing,
        "unreadable": unreadable,
        "readable": len(metric_list),
        "completed_state": completed_state,
    }
    result.update(summarize(metric_list, tool_counter))

    if args.pretty:
        print(json.dumps(result, indent=2, sort_keys=False))
    else:
        print(json.dumps(result, separators=(",", ":"), sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
