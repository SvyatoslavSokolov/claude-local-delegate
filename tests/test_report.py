"""Tests for contrib/report.py (delegation pipeline analytics CLI)."""
import csv
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPORT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "contrib", "report.py")


def _load_report():
    spec = importlib.util.spec_from_file_location("report_under_test", REPORT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


report = _load_report()


def _mk_state_dir(tmp, now_ts):
    """Synthetic state dir: 3 runs (2 rated, 1 blocked), 1 verified cycle, 1 batch,
    no coordination.sqlite3. Crafted so the 'blocked' category crosses BLOCKED_PCT
    (1/3 = 33% > 15%) and a rated cell has worth_it < LOW_WORTH."""
    os.makedirs(os.path.join(tmp, "verified"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "batches"), exist_ok=True)
    events = []
    # run A: rated, low worth_it (fires LOW_WORTH insight)
    events.append({"event": "spawn", "run_id": "aaaa0001", "task_id": None,
                   "task_key": "k-a", "name": "run-a", "complexity": "medium",
                   "est_minutes": 5, "blocks": [], "profile": "think",
                   "model": "m", "prompt_chars": 100, "read_only": True,
                   "cwd": "/proj/a", "at": now_ts - 3600})
    events.append({"event": "rate", "run_id": "aaaa0001", "quality": 70,
                   "worth_it": 30, "note": "not worth it", "at": now_ts - 3500,
                   "stats": {"api_calls": 12, "output_tokens": 500,
                             "total_input_tokens": 20000, "cached_input_tokens": 9000,
                             "first_call_input_tokens": 10000, "peak_context": 15000,
                             "thinking_chars": 10, "text_chars": 20, "tool_calls": 5,
                             "duration_s": 300}})
    # run B: rated, high scores
    events.append({"event": "spawn", "run_id": "bbbb0002", "task_id": "t2",
                   "task_key": "k-b", "name": "run-b", "complexity": "small",
                   "est_minutes": 2, "blocks": [], "profile": "fast",
                   "model": "m", "prompt_chars": 50, "read_only": False,
                   "cwd": "/proj/b", "at": now_ts - 7200})
    events.append({"event": "rate", "run_id": "bbbb0002", "quality": 90,
                   "worth_it": 95, "note": "great", "at": now_ts - 7100,
                   "stats": {"api_calls": 3, "output_tokens": 80,
                             "total_input_tokens": 3000, "cached_input_tokens": 0,
                             "first_call_input_tokens": 3000, "peak_context": 2500,
                             "thinking_chars": 0, "text_chars": 40, "tool_calls": 1,
                             "duration_s": 120}})
    # run C: spawned but blocked, not rated
    events.append({"event": "spawn", "run_id": "cccc0003", "task_id": None,
                   "task_key": "k-c", "name": "run-c", "complexity": "large",
                   "est_minutes": 30, "blocks": [], "profile": "think",
                   "model": "m", "prompt_chars": 200, "read_only": True,
                   "cwd": "/proj/c", "at": now_ts - 1800})
    events.append({"event": "blocked", "run_id": "cccc0003",
                   "category": "tool-deny", "note": "blocked", "at": now_ts - 1700})
    with open(os.path.join(tmp, "metrics.jsonl"), "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    # verified cycle: run A as current worker, iteration 2, 1 history entry
    with open(os.path.join(tmp, "verified", "vv01.json"), "w", encoding="utf-8") as fh:
        json.dump({"vid": "vv01", "iteration": 2, "current_worker_id": "aaaa0001",
                   "history": [{"worker_id": "zzz"}]}, fh)
    # batch containing run B
    with open(os.path.join(tmp, "batches", "bb01.json"), "w", encoding="utf-8") as fh:
        json.dump({"batch_id": "bb01", "agent_ids": ["bbbb0002"],
                   "created_at": now_ts - 7200}, fh)
    return tmp


class TestReport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.now_ts = time.time()  # anchor to real time so --days 30 keeps all runs
        self.state = _mk_state_dir(self.tmp, self.now_ts)
        self.csv_path = os.path.join(self.tmp, "out.csv")

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = report.main(list(args) + ["--state-dir", self.state])
        return rc, buf.getvalue()

    def test_csv_header_and_rows(self):
        rc, _ = self._main("--csv", self.csv_path, "--days", "30")
        self.assertEqual(rc, 0)
        with open(self.csv_path, encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader)
            rows = list(reader)
        self.assertEqual(header, report.CSV_COLUMNS)
        self.assertEqual(
            report.CSV_COLUMNS,
            ["run_id", "task_id", "task_key", "name", "project", "cwd",
             "created_at", "finished_at", "duration_s",
             "complexity", "est_minutes", "profile", "model", "read_only",
             "prompt_chars", "quality", "worth_it", "api_calls", "output_tokens",
             "input_tokens", "cached_input_tokens", "peak_context",
             "thinking_chars", "text_chars", "tool_calls", "blocked_category",
             "n_blocked", "verified", "verified_iterations", "batch_id",
             "task_status"])
        self.assertEqual(len(rows), 3)
        by_run = {r[0]: r for r in rows}
        self.assertEqual(len(by_run), 3)
        idx = {c: i for i, c in enumerate(report.CSV_COLUMNS)}
        # rated rows keep quality/worth_it
        self.assertEqual(by_run["aaaa0001"][idx["quality"]], "70")
        self.assertEqual(by_run["aaaa0001"][idx["worth_it"]], "30")
        self.assertEqual(by_run["bbbb0002"][idx["quality"]], "90")
        self.assertEqual(by_run["bbbb0002"][idx["worth_it"]], "95")
        # input_tokens maps from total_input_tokens
        self.assertEqual(by_run["aaaa0001"][idx["input_tokens"]], "20000")
        # unrated row has empty rating fields
        self.assertEqual(by_run["cccc0003"][idx["quality"]], "")
        self.assertEqual(by_run["cccc0003"][idx["duration_s"]], "")
        # blocked category from last blocked event
        self.assertEqual(by_run["cccc0003"][idx["blocked_category"]], "tool-deny")
        self.assertEqual(by_run["cccc0003"][idx["n_blocked"]], "1")
        # verified join + batch join
        self.assertEqual(by_run["aaaa0001"][idx["verified"]], "1")
        self.assertEqual(by_run["aaaa0001"][idx["verified_iterations"]], "2")
        self.assertEqual(by_run["bbbb0002"][idx["batch_id"]], "bb01")

    def test_no_sqlite_no_raise_and_empty_task_status(self):
        rc, _ = self._main("--csv", self.csv_path, "--days", "30")
        self.assertEqual(rc, 0)
        with open(self.csv_path, encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                self.assertEqual(r["task_status"], "")

    def test_json_path(self):
        rc, out = self._main("--json", "--days", "30")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        for key in ("cohort", "trend", "health", "insights", "lowest"):
            self.assertIn(key, data)

    def test_insights_fire_for_blocked_category(self):
        rc, out = self._main("--days", "30")
        self.assertEqual(rc, 0)
        rep = report.compute_report(self.state, days=30)
        self.assertIsInstance(rep["insights"], list)
        self.assertTrue(all(isinstance(s, str) for s in rep["insights"]))
        self.assertTrue(rep["insights"])
        joined = "\n".join(rep["insights"])
        # blocked tool-deny is 1/3 = 33% > 15%
        self.assertIn("blocked tool-deny", joined)
        # low worth_it cell fires too (30 < 50)
        self.assertIn("mean worth_it", joined)
        # terminal output contains all 5 sections
        for section in ("1) COHORT", "2) TREND", "3) HEALTH-INDEX",
                        "4) INSIGHTS", "5) LOWEST"):
            self.assertIn(section, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)