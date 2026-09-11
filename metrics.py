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