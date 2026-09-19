#!/usr/bin/env python3
"""Cluster and GPU telemetry collector for vLLM and LiteLLM gateway.

Monitors:
1. vLLM Prometheus metrics (running requests, queue depth, KV cache %, prefix cache hit rate).
2. Active GPU concurrency and contention index.
3. LiteLLM proxy status and available models.
"""

import json
import os
import re
import time
import urllib.request
from typing import Any, Dict, Optional

DEFAULT_VLLM_METRICS_URL = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_VLLM_METRICS_URL",
    "http://192.168.1.109:8000/metrics",
)
DEFAULT_GATEWAY_URL = os.environ.get(
    "CLAUDE_LOCAL_DELEGATE_GATEWAY_URL",
    "http://192.168.1.109:4000",
)

# In-memory short-lived cache (3 seconds) to prevent spamming the cluster
_TELEMETRY_CACHE: Optional[Dict[str, Any]] = None
_TELEMETRY_CACHE_TIME: float = 0.0
_CACHE_TTL_SECONDS: float = 3.0


def fetch_vllm_telemetry(
    url: str = DEFAULT_VLLM_METRICS_URL,
    timeout: float = 2.5,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Fetch and parse live GPU & vLLM Prometheus metrics."""
    global _TELEMETRY_CACHE, _TELEMETRY_CACHE_TIME
    now = time.time()
    if use_cache and _TELEMETRY_CACHE is not None and (now - _TELEMETRY_CACHE_TIME) < _CACHE_TTL_SECONDS:
        return _TELEMETRY_CACHE

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "claude-local-delegate-telemetry"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")

        def find_metric(metric_prefix: str) -> Optional[float]:
            pattern = rf"^{metric_prefix}(?:\{{[^}}]*\}})?\s+([\d\.e\+\-]+)"
            m = re.search(pattern, text, re.MULTILINE)
            return float(m.group(1)) if m else None

        running = find_metric("vllm:num_requests_running") or 0.0
        waiting = find_metric("vllm:num_requests_waiting") or 0.0
        kv_usage = (find_metric("vllm:kv_cache_usage_perc") or 0.0) * 100.0
        gen_tokens = find_metric("vllm:generation_tokens_total") or 0.0

        # Cache hits vs local compute prefill tokens
        hit_pattern = r'vllm:prompt_tokens_by_source_total\{[^}]*source="local_cache_hit"[^}]*\}\s+([\d\.e\+\-]+)'
        hit_m = re.search(hit_pattern, text)
        hit_tokens = float(hit_m.group(1)) if hit_m else 0.0

        comp_pattern = r'vllm:prompt_tokens_by_source_total\{[^}]*source="local_compute"[^}]*\}\s+([\d\.e\+\-]+)'
        comp_m = re.search(comp_pattern, text)
        comp_tokens = float(comp_m.group(1)) if comp_m else 0.0

        total_prompt = hit_tokens + comp_tokens
        cache_hit_pct = round((hit_tokens / total_prompt * 100.0), 1) if total_prompt > 0 else 0.0

        result = {
            "healthy": True,
            "endpoint": url,
            "running_requests": int(running),
            "waiting_requests": int(waiting),
            "contention": "high" if waiting > 2 else ("moderate" if waiting > 0 else "none"),
            "kv_cache_usage_pct": round(kv_usage, 1),
            "cache_hit_rate_pct": cache_hit_pct,
            "prompt_tokens_total": int(total_prompt),
            "generation_tokens_total": int(gen_tokens),
            "fetched_at": now,
        }
        _TELEMETRY_CACHE = result
        _TELEMETRY_CACHE_TIME = now
        return result

    except Exception as exc:
        return {
            "healthy": False,
            "endpoint": url,
            "error": str(exc),
            "running_requests": 0,
            "waiting_requests": 0,
            "contention": "unknown",
            "kv_cache_usage_pct": 0.0,
            "cache_hit_rate_pct": 0.0,
            "prompt_tokens_total": 0,
            "generation_tokens_total": 0,
            "fetched_at": now,
        }


def check_cluster_overview(
    metrics_url: str = DEFAULT_VLLM_METRICS_URL,
    gateway_url: str = DEFAULT_GATEWAY_URL,
) -> Dict[str, Any]:
    """Combine vLLM hardware metrics and LiteLLM gateway status."""
    vllm = fetch_vllm_telemetry(metrics_url)
    gateway_ok = False
    gateway_status = "unreachable"
    try:
        req = urllib.request.Request(gateway_url.rstrip("/") + "/health/readiness")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status == 200:
                gateway_ok = True
                gateway_status = "healthy"
    except Exception:
        try:
            with urllib.request.urlopen(gateway_url.rstrip("/"), timeout=2.0) as resp:
                gateway_ok = True
                gateway_status = f"HTTP {resp.status}"
        except Exception as e2:
            gateway_status = str(e2)

    return {
        "vllm": vllm,
        "gateway": {
            "url": gateway_url,
            "healthy": gateway_ok,
            "status": gateway_status,
        },
        "overall_healthy": vllm.get("healthy", False) and gateway_ok,
    }


if __name__ == "__main__":
    overview = check_cluster_overview()
    print(json.dumps(overview, indent=2))
