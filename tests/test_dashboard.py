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
        self.assertIn("spawnModal", html)
        self.assertIn("spawnAdapter", html)
        self.assertIn("spawnModel", html)
        self.assertIn("submitSpawn", html)
        self.assertIn("/api/task/spawn", html)
        self.assertIn("quotasTab", html)
        self.assertIn("adapterQuotaBanner", html)
        self.assertIn("spawnProjectSelect", html)
        self.assertIn("claudeQuotaPill", html)

    def test_compile_quotas_structure(self):
        quotas = dashboard.compile_quotas()
        self.assertIsInstance(quotas, dict)
        for key in ("claude", "agy", "codex", "local"):
            self.assertIn(key, quotas)
            q = quotas[key]
            self.assertIn("label", q)
            self.assertIn("used_5h", q)
            self.assertIn("rem_5h", q)
            self.assertIn("used_7d", q)
            self.assertIn("rem_7d", q)
            self.assertIn("reset_in", q)
            if key in ("claude", "agy", "codex"):
                self.assertEqual(q["limit_5h"], 0)
                self.assertEqual(q["limit_7d"], 0)
                self.assertEqual(q["rem_5h"], 0)
                self.assertEqual(q["rem_7d"], 0)

    def test_get_known_projects(self):
        projects = dashboard.get_known_projects()
        self.assertIsInstance(projects, list)
        self.assertGreater(len(projects), 0)
        for p in projects:
            self.assertIn("name", p)
            self.assertIn("path", p)

    def test_compile_overview_includes_quotas_and_projects(self):
        with patch("cluster_telemetry.check_cluster_overview") as mock_cl:
            mock_cl.return_value = {"overall_healthy": True, "vllm": {"running_requests": 0}}
            overview = dashboard.compile_overview()
            self.assertIn("quotas", overview)
            self.assertIn("known_projects", overview)
            self.assertIn("claude", overview["quotas"])

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

    def test_spawn_task_from_dashboard(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/coordination.sqlite3"
            codex_state = f"{tmpdir}/codex_runs"

            with patch("dashboard.DB_PATH", db_path), \
                 patch("dashboard.CODEX_STATE_DIR", codex_state):

                # 1. Validation errors
                ok, res = dashboard.spawn_task_from_dashboard("local", "")
                self.assertFalse(ok)
                self.assertIn("empty", res["error"])

                ok, res = dashboard.spawn_task_from_dashboard("unsupported_adapter", "do something")
                self.assertFalse(ok)
                self.assertIn("Unsupported", res["error"])

                ok, res = dashboard.spawn_task_from_dashboard("local", "do something", cwd="/non/existent/path")
                self.assertFalse(ok)
                self.assertIn("does not exist", res["error"])

                # 2. Local adapter spawn
                with patch("server._spawn_native_agent") as mock_spawn_local:
                    mock_spawn_local.return_value = ("loc-run-123", None)
                    ok, res = dashboard.spawn_task_from_dashboard(
                        adapter="local",
                        task="Refactor telemetry",
                        model="qwen2.5-coder-32b",
                        cwd=tmpdir,
                        summary="Refactor telemetry task",
                    )
                    self.assertTrue(ok)
                    self.assertEqual(res["run_id"], "loc-run-123")
                    self.assertEqual(res["adapter"], "local")
                    self.assertEqual(res["model"], "qwen2.5-coder-32b")

                    # Verify task was claimed on Board
                    tasks = dashboard.load_tasks()
                    self.assertEqual(len(tasks), 1)
                    self.assertEqual(tasks[0]["adapter"], "local")
                    self.assertEqual(tasks[0]["runs"], ["loc-run-123"])

                # 3. AGY adapter spawn
                with patch("adapters.agy.agy_delegate.AgyDelegateManager.spawn") as mock_agy_spawn:
                    mock_agy_spawn.return_value = {"run_id": "agy-abc-999", "status": "running"}
                    ok, res = dashboard.spawn_task_from_dashboard(
                        adapter="agy",
                        task="Research papers",
                        model="gemini-2.5-pro",
                        cwd=tmpdir,
                    )
                    self.assertTrue(ok)
                    self.assertEqual(res["run_id"], "agy-abc-999")
                    self.assertEqual(res["adapter"], "agy")
                    tasks = dashboard.load_tasks()
                    self.assertEqual(len(tasks), 2)
                    agy_task = [t for t in tasks if t["adapter"] == "agy"][0]
                    self.assertEqual(agy_task["runs"], ["agy-abc-999"])

                # 4. Codex adapter spawn
                with patch("subprocess.Popen") as mock_popen:
                    mock_proc = MagicMock()
                    mock_proc.pid = 98765
                    mock_popen.return_value = mock_proc

                    ok, res = dashboard.spawn_task_from_dashboard(
                        adapter="codex",
                        task="Write unit test",
                        model="o3-mini",
                        cwd=tmpdir,
                    )
                    self.assertTrue(ok)
                    self.assertTrue(res["run_id"].startswith("codex-"))
                    self.assertEqual(res["adapter"], "codex")
                    self.assertEqual(res["model"], "o3-mini")

                # 5. Direct Claude Code adapter spawn (Anthropic subscription)
                with patch("subprocess.run") as mock_run:
                    mock_res = MagicMock()
                    mock_res.returncode = 0
                    mock_res.stdout = b"Agent abcdef12 backgrounded.\n"
                    mock_run.return_value = mock_res

                    ok, res = dashboard.spawn_task_from_dashboard(
                        adapter="claude",
                        task="Design system architecture",
                        model="claude-3-7-sonnet",
                        cwd=tmpdir,
                    )
                    self.assertTrue(ok)
                    self.assertEqual(res["adapter"], "claude")
                    self.assertEqual(res["model"], "claude-3-7-sonnet")
                    self.assertEqual(res["run_id"], "abcdef12")


if __name__ == "__main__":
    unittest.main()


