"""Live state of the inference engine behind the LiteLLM gateway (Strata).

The gateway (ANTHROPIC_BASE_URL) hides how many requests the engine really runs
at once. Strata serves ONE request at a time unless it was started with batch
slots, so N delegates share one decode stream: the right planning unit is
"how many agents interleave well", not a token budget. This module reads the
engine's own /v1/status and /metrics (Strata API; llama.cpp-style names) so the
pool snapshot and the prompt hook can say what is actually happening.

The engine URL comes from CLAUDE_LOCAL_DELEGATE_ENGINE_URL (process env first,
then the delegate settings profile env). Unset -> {'known': False}. Never raises.
"""
import json
import os
import urllib.request
from pathlib import Path

ENGINE_URL_KEY = "CLAUDE_LOCAL_DELEGATE_ENGINE_URL"
# Agents spend part of each turn in tools (shell, tests), so a few of them
# interleave on one decode stream without each slowing down N-fold.
DEEP_AGENTS_PER_STREAM = 2


def _profile_env(settings_path):
    try:
        return json.loads(Path(settings_path).expanduser().read_text()).get("env", {}) or {}
    except (OSError, ValueError):
        return {}


def engine_url(settings_path):
    env = _profile_env(settings_path)
    url = os.environ.get(ENGINE_URL_KEY) or env.get(ENGINE_URL_KEY) or ""
    return url.rstrip("/"), env.get("ANTHROPIC_API_KEY", "")


def _get(url, key, timeout):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def snapshot(settings_path, timeout=3.0):
    """Compact engine state, or {'known': False, 'reason': ...}."""
    base, key = engine_url(settings_path)
    if not base:
        return {"known": False, "reason": f"{ENGINE_URL_KEY} not set"}
    try:
        status = _get(base + "/v1/status", key, timeout)
    except Exception as exc:  # offline, auth, bad JSON: report, never raise
        return {"known": False, "reason": f"engine status unreachable: {exc}"}
    try:
        metrics = _get(base + "/metrics", key, timeout)
    except Exception:
        metrics = {}
    live = metrics.get("live") or {}
    cache = metrics.get("conversation_cache") or {}
    timings = status.get("last_timings") or {}
    serving = (status.get("concurrency") or {}).get("serving") or 1
    slots = cache.get("slots") if cache.get("enabled") else 0
    return {
        "known": True,
        "engine": status.get("service"),
        "version": status.get("engine"),
        "model": status.get("model"),
        "loaded": bool(status.get("loaded")),
        "context_max": status.get("cache_max_tokens"),
        "serving": serving,
        "state": live.get("state"),
        "queued": live.get("queued") or 0,
        "decode_tok_s": round(timings["predicted_per_second"], 1) if timings.get("predicted_per_second") else None,
        "conversation_cache": {
            "slots": slots or 0,
            "parked": cache.get("parked", 0),
            "evictions": cache.get("evictions", 0),
            "reused_tokens": cache.get("reused_tokens", 0),
        },
        "recommended_live_agents": serving * DEEP_AGENTS_PER_STREAM,
    }


def advice(snap, running):
    """One line for the hook / pool note. `running` = live local delegates."""
    if not snap.get("known"):
        return ""
    if not snap.get("loaded"):
        return "Local engine is loading the model: delegates will wait until it is up."
    rec = snap["recommended_live_agents"]
    slots = snap["conversation_cache"]["slots"]
    speed = f" ~{snap['decode_tok_s']:.0f} tok/s" if snap.get("decode_tok_s") else ""
    head = (f"Local engine {snap.get('engine')} {snap.get('version')}: serves {snap['serving']} "
            f"request(s) at a time{speed}, queue {snap['queued']}, "
            f"{snap['conversation_cache']['parked']}/{slots} conversations cached.")
    if slots and running >= slots:
        return head + (f" {running} live delegates exceed the {slots} cached conversations: "
                       "each switch re-reads the whole context. Let some finish first.")
    if running >= rec:
        return head + f" {running} live delegates already share it; more will only queue."
    return head + (f" Room for {rec - running} more deep delegate(s) "
                   "(deep_dive / context_pack / review_diff), not many small scouts.")
