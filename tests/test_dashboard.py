"""Unit tests for dashboard.py web GUI and API endpoints."""

import json
import unittest
from unittest.mock import MagicMock, patch

import dashboard


class DashboardTests(unittest.TestCase):
    def test_compile_overview_structure(self):
        with patch("cluster_telemetry.check_cluster_overview") as mock_cl:
            mock_cl.return_value = {"overall_healthy": True, "vllm": {"running_requests": 0}}
            overview = dashboard.compile_overview()
            self.assertIn("tasks", overview)
            self.assertIn("runs_count", overview)
            self.assertIn("avg_quality", overview)
            self.assertIn("total_usd_saved", overview)
            self.assertIn("cluster", overview)

    def test_compile_runs_summary_includes_metrics(self):
        runs = dashboard.compile_runs_summary()
        self.assertIsInstance(runs, list)
        if runs:
            first = runs[0]
            self.assertIn("run_id", first)
            self.assertIn("model", first)
            self.assertIn("usd_saved", first)

    def test_html_template_has_vital_elements(self):
        html = dashboard.HTML_TEMPLATE
        self.assertIn("Claude Local Delegate", html)
        self.assertIn("/api/overview", html)
        self.assertIn("vllmStatus", html)
        self.assertIn("runsTableBody", html)


if __name__ == "__main__":
    unittest.main()
