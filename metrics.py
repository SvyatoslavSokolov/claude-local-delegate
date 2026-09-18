#!/usr/bin/env python3
"""Append-only delegation metrics ledger and transcript stats.

Every delegation records a "spawn" event (planning metadata) and, after the
supervisor reviews the result, a "rate" event (two 0-100 scores plus transcript
stats). Both are written to a single JSONL file under the state dir so the whole
delegation history can be analyzed later.

Stdlib only. Nothing here may ever break a spawn: the ledger append is
best-effort and swallows I/O errors, and the transcript reader tolerates
malformed lines.
"""

import json
import os
import re
import time
from datetime import datetime, timezone

# datetime.fromisoformat (Python 3.8) only accepts 0, 3 or 6 fractional-second
# digits. Transcript timestamps vary in precision, so normalize any fractional
# group to exactly 6 digits before parsing. (Copied from contrib/history_stats.py.)
_FRAC_RE = re.compile(r"\.(\d+)")


def _pad_frac(match):
    digits = (match.group(1) + "000000")[:6]
    return "." + digits


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
        s = _FRAC_RE.sub(_pad_frac, s)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


COMPLEXITIES = ("trivial", "small", "medium", "large")
_PROFILES = ("fast", "think")


def profile_for(complexity, explicit_profile):
    """Map a complexity + explicit profile to the effective "fast"/"think" profile.

    An explicit, valid profile always wins. Otherwise trivial/small run "fast"
    (thinking off) and medium/large/None run "think".
    """
    if explicit_profile in _PROFILES:
        return explicit_profile
    if complexity in ("trivial", "small"):
        return "fast"
    return "think"


def append_event(state_dir, event):
    """Append one JSON line to <state_dir>/metrics.jsonl. Best-effort: it must
    never break a spawn, so every I/O or serialization failure is swallowed."""
    try:
        entry = dict(event)
        entry["at"] = time.time()
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, "metrics.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (OSError, ValueError, TypeError):
        pass


def transcript_stats(path):
    """Stream a Claude Code JSONL transcript and return aggregate stats.

    One API response is split across several JSONL entries that share
    ``message.id`` and each repeats the same ``usage`` -- so usage is deduped by
    ``message.id`` (the last usage seen per id wins) and counted exactly once.
    Content blocks (thinking/text/tool_use) accumulate across the entries that
    carry them; tool_use blocks are deduped by their block id.
    """
    usage_by_id = {}
    first_seen_order = []
    seen_ids = set()
    anon = 0
    tool_use_ids = set()
    tool_use_no_id = 0
    thinking_chars = 0
    text_chars = 0
    first_ts = None
    last_ts = None

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
            if ts is not None:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            if event.get("type") != "assistant":
                continue
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            mid = message.get("id")
            if mid is None:
                anon += 1
                mid = "_anon_%d" % anon
            usage = message.get("usage")
            if isinstance(usage, dict):
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    first_seen_order.append(mid)
                usage_by_id[mid] = usage  # last usage seen per id wins
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "thinking":
                        txt = block.get("thinking")
                        if isinstance(txt, str):
                            thinking_chars += len(txt)
                    elif btype == "text":
                        txt = block.get("text")
                        if isinstance(txt, str):
                            text_chars += len(txt)
                    elif btype == "tool_use":
                        bid = block.get("id")
                        if bid is not None:
                            tool_use_ids.add(bid)
                        else:
                            tool_use_no_id += 1

    output_tokens = 0
    total_input_tokens = 0
    cached_input_tokens = 0
    peak_context = 0
    first_call_input_tokens = 0
    for i, mid in enumerate(first_seen_order):
        u = usage_by_id[mid]
        inp = u.get("input_tokens") or 0
        cr = u.get("cache_read_input_tokens") or 0
        cc = u.get("cache_creation_input_tokens") or 0
        out = u.get("output_tokens") or 0
        total_in = inp + cr + cc
        output_tokens += out
        total_input_tokens += total_in
        cached_input_tokens += cr
        if i == 0:
            first_call_input_tokens = total_in
        if total_in > peak_context:
            peak_context = total_in

    duration_s = 0.0
    if first_ts is not None and last_ts is not None:
        duration_s = last_ts - first_ts
        if duration_s < 0:
            duration_s = 0.0

    return {
        "api_calls": len(first_seen_order),
        "output_tokens": output_tokens,
        "total_input_tokens": total_input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "first_call_input_tokens": first_call_input_tokens,
        "peak_context": peak_context,
        "thinking_chars": thinking_chars,
        "text_chars": text_chars,
        "tool_calls": len(tool_use_ids) + tool_use_no_id,
        "duration_s": round(duration_s, 2),
    }


# ---------------------------------------------------------------------------
# Shared readers for the delegation history. report.py (and any future
# analytics tool) imports these instead of re-parsing the state dir, so the
# ledger/verified/batch formats live in exactly one place. All readers are
# read-only and tolerate a missing dir/file (they return an empty structure,
# never raise).
# ---------------------------------------------------------------------------

def percentile(values, p):
    """Deterministic nearest-rank percentile. None for empty input."""
    if not values:
        return None
    import math
    ordered = sorted(values)
    rank = int(math.ceil(p / 100.0 * len(ordered)))
    rank = max(1, min(len(ordered), rank))
    return ordered[rank - 1]


def load_ledger(state_dir):
    """Join the metrics ledger by run_id.

    Returns {run_id: {"spawn": dict|None, "rate": dict|None,
                      "blocked": [dict, ...], "n_blocked": int}} for every run
    seen. "spawn" is the first spawn event; "rate" is the LAST rate event
    (a run is rated once, but a re-rate would win); "blocked" collects every
    blocked event in order. Tolerates blank/garbage lines and a missing file
    (returns {}).
    """
    path = os.path.join(state_dir, "metrics.jsonl")
    if not os.path.exists(path):
        return {}
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
            entry = runs.setdefault(run_id, {"spawn": None, "rate": None,
                                             "blocked": [], "n_blocked": 0})
            kind = event.get("event")
            if kind == "spawn" and entry["spawn"] is None:
                entry["spawn"] = event
            elif kind == "rate":
                entry["rate"] = event  # later rate wins
            elif kind == "blocked":
                entry["blocked"].append(event)
                entry["n_blocked"] += 1
    return runs


def load_verified(state_dir):
    """Read every verified/ work->check->revise cycle.

    Returns {vid: dict} where dict carries the on-disk fields (phase,
    iteration, current_worker_id, final_answer, failure_report, coordination_task_id,
    ...). The on-disk values are JSON-decoded (so None/ints come back as real
    values, not the strings 'None'/'3'). Missing dir -> {}.
    """
    base = os.path.join(state_dir, "verified")
    out = {}
    if not os.path.isdir(base):
        return out
    for name in os.listdir(base):
        if not name.endswith(".json"):
            continue
        path = os.path.join(base, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out[data.get("vid") or name[:-5]] = data
    return out


def load_batches(state_dir):
    """Read every fan_out batch.

    Returns {batch_id: {"agent_ids": [str, ...], "created_at": float|None}}.
    A batch is considered successful when it has at least one agent. Missing
    dir -> {}.
    """
    base = os.path.join(state_dir, "batches")
    out = {}
    if not os.path.isdir(base):
        return out
    for name in os.listdir(base):
        if not name.endswith(".json"):
            continue
        path = os.path.join(base, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        batch_id = data.get("batch_id") or name[:-5]
        agents = data.get("agent_ids") or []
        out[batch_id] = {
            "agent_ids": [a for a in agents if isinstance(a, str)],
            "created_at": parse_timestamp(data.get("created_at")),
        }
    return out