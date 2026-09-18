"""Tests for Antigravity (agy) delegation integration."""
import json
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile

from agy_delegate import AgyDelegateManager
import server


class AgyDelegateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = AgyDelegateManager(state_dir=self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_manager_spawn_and_meta(self):
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_popen.return_value = mock_proc

            meta = self.mgr.spawn("echo hello", model="gemini-3.8-flash-high")
            self.assertEqual(meta["pid"], 12345)
            self.assertEqual(meta["status"], "running")
            self.assertEqual(meta["model"], "gemini-3.8-flash-high")

            # Check meta file written
            run_file = Path(self.tmp.name) / f"{meta['run_id']}.json"
            self.assertTrue(run_file.exists())
            saved = json.loads(run_file.read_text())
            self.assertEqual(saved["run_id"], meta["run_id"])

    def test_manager_check_status_completed(self):
        run_id = "agy-test001"
        meta_file = Path(self.tmp.name) / f"{run_id}.json"
        out_file = Path(self.tmp.name) / f"{run_id}.out"

        meta_file.write_text(json.dumps({
            "run_id": run_id,
            "pid": 9999999,
            "task": "test",
            "start_time": 1000.0,
            "status": "running"
        }))

        out_file.write_text(json.dumps({
            "conversation_id": "conv-123",
            "status": "SUCCESS",
            "response": "Answer is 42",
            "duration_seconds": 2.5,
            "num_turns": 1,
            "usage": {"total_tokens": 100}
        }))

        status = self.mgr.check_status(run_id)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["response"], "Answer is 42")
        self.assertEqual(status["conversation_id"], "conv-123")

    def test_server_handlers(self):
        with patch.object(server, "_get_agy_mgr") as mock_get_mgr:
            mock_mgr = MagicMock()
            mock_get_mgr.return_value = mock_mgr

            # 1. start
            mock_mgr.spawn.return_value = {
                "run_id": "agy-1234",
                "model": "gemini-3.8-flash-high",
                "pid": 5555
            }
            res = server.start_agy_delegate({"task": "hello"})
            self.assertFalse(res["isError"])
            self.assertIn("agy-1234", res["content"][0]["text"])

            # 2. check
            mock_mgr.check_status.return_value = {"status": "running", "elapsed_seconds": 5}
            res_check = server.check_agy_status({"run_id": "agy-1234"})
            self.assertFalse(res_check["isError"])
            self.assertIn("running", res_check["content"][0]["text"])

            # 3. result completed
            mock_mgr.get_result.return_value = {
                "status": "completed",
                "response": "Done!",
                "conversation_id": "c-1",
                "agy_duration": 3.0,
                "usage": {"total_tokens": 50}
            }
            res_result = server.get_agy_result({"run_id": "agy-1234"})
            self.assertFalse(res_result["isError"])
            self.assertIn("Done!", res_result["content"][0]["text"])

            # 4. stop
            mock_mgr.stop.return_value = {"status": "stopped"}
            res_stop = server.stop_agy({"run_id": "agy-1234"})
            self.assertFalse(res_stop["isError"])

            # 5. show_agent_tree
            res_tree = server.show_agent_tree_handler({})
            self.assertFalse(res_tree["isError"])
            self.assertIn("MULTI-AGENT LOCAL HARNESS MONITOR", res_tree["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
