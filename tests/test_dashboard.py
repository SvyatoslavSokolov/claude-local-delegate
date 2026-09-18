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
        self.assertIn("tasksWaiting", html)
        self.assertIn("tasksCancelled", html)
        self.assertIn("cleanupStale", html)
        self.assertIn("updateTask", html)

    def test_update_and_cleanup_tasks(self):
        import tempfile
        from coordination import Board
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/coordination.sqlite3"
            board = Board(db_path)
            claim = board.claim("worker-1", {"project": tmpdir, "task_key": "task-test-1", "mode": "write"})
            task_id = claim["task"]["id"]

            with patch("dashboard.DB_PATH", db_path):
                # Test invalid status
                ok, err = dashboard.update_task_status(task_id, "invalid_status")
                self.assertFalse(ok)

                # Test pause
                ok, err = dashboard.update_task_status(task_id, "paused", "taking a break")
                self.assertTrue(ok)
                tasks = dashboard.load_tasks()
                paused = [t for t in tasks if t["id"] == task_id][0]
                self.assertEqual(paused["status"], "paused")
                self.assertIn("taking a break", paused["note"])

                # Test mark done
                ok, err = dashboard.update_task_status(task_id, "done")
                self.assertTrue(ok)
                tasks = dashboard.load_tasks()
                done = [t for t in tasks if t["id"] == task_id][0]
                self.assertEqual(done["status"], "done")

                # Test cleanup_stale_tasks: create an old active task
                claim2 = board.claim("worker-2", {"project": tmpdir, "task_key": "task-test-2", "mode": "write"})
                task2_id = claim2["task"]["id"]
                # Artificial backdating of updated_at
                import sqlite3, json, time
                conn = sqlite3.connect(db_path)
                c = conn.cursor()
                c.execute("SELECT rowid, body FROM tasks WHERE id = ?;", (task2_id,))
                rowid, b_str = c.fetchone()
                body = json.loads(b_str)
                body["updated_at"] = time.time() - 100000
                c.execute("UPDATE tasks SET body = ? WHERE rowid = ?;", (json.dumps(body), rowid))
                conn.commit()
                conn.close()

                cleaned = dashboard.cleanup_stale_tasks(max_age_hours=1.0, target_status="cancelled")
                self.assertGreaterEqual(cleaned, 1)

                tasks = dashboard.load_tasks()
                t2 = [t for t in tasks if t["id"] == task2_id][0]
                self.assertEqual(t2["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()

