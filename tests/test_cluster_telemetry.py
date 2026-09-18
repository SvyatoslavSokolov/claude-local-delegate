"""Unit tests for cluster_telemetry.py."""

import io
import unittest
from unittest.mock import MagicMock, patch

import cluster_telemetry


SAMPLE_PROMETHEUS = """
# HELP vllm:num_requests_running Number of requests
vllm:num_requests_running{engine="0",model_name="default_model"} 2.0
# HELP vllm:num_requests_waiting Number of requests waiting
vllm:num_requests_waiting{engine="0",model_name="default_model"} 1.0
# HELP vllm:kv_cache_usage_perc KV-cache usage
vllm:kv_cache_usage_perc{engine="0",model_name="default_model"} 0.42
# HELP vllm:generation_tokens_total Number of generation tokens
vllm:generation_tokens_total{engine="0",model_name="default_model"} 5000.0
# HELP vllm:prompt_tokens_by_source_total
vllm:prompt_tokens_by_source_total{engine="0",model_name="default_model",source="local_cache_hit"} 90000.0
vllm:prompt_tokens_by_source_total{engine="0",model_name="default_model",source="local_compute"} 10000.0
"""


class ClusterTelemetryTests(unittest.TestCase):
    def test_fetch_vllm_telemetry_parses_metrics(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = SAMPLE_PROMETHEUS.encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            data = cluster_telemetry.fetch_vllm_telemetry(use_cache=False)
            self.assertTrue(data["healthy"])
            self.assertEqual(data["running_requests"], 2)
            self.assertEqual(data["waiting_requests"], 1)
            self.assertEqual(data["contention"], "moderate")
            self.assertEqual(data["kv_cache_usage_pct"], 42.0)
            self.assertEqual(data["cache_hit_rate_pct"], 90.0)
            self.assertEqual(data["prompt_tokens_total"], 100000)
            self.assertEqual(data["generation_tokens_total"], 5000)

    def test_fetch_vllm_telemetry_handles_network_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            data = cluster_telemetry.fetch_vllm_telemetry(use_cache=False)
            self.assertFalse(data["healthy"])
            self.assertIn("Connection refused", data["error"])
            self.assertEqual(data["running_requests"], 0)


if __name__ == "__main__":
    unittest.main()
