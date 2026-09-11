"""Unit tests for contrib/delegate_report.py and the history_stats dedupe fix."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "contrib"))

import delegate_report as dr  # noqa: E402
import history_stats as hs  # noqa: E402


def spawn_event(run_id, **kw):
    e = {"event": "spawn", "at": 1000, "run_id": run_id,
         "task_id": 1, "name": "t-" + run_id, "complexity": None,
         "est_minutes": None, "blocks": [], "profile": None,
         "model": "x", "prompt_chars": 10, "read_only": True,
         "cwd": "/tmp"}
    e.update(kw)
    return e


def rate_event(run_id, **kw):
    e = {"event": "rate", "at": 2000, "run_id": run_id,
         "quality": None, "worth_it": None, "note": None, "stats": None}
    e.update(kw)
    return e


def stats(duration_s, out=1000, tc=100, xc=100):
    return {"duration_s": duration_s, "api_calls": 1, "output_tokens": out,
            "total_input_tokens": 5, "cached_input_tokens": 0,
            "first_call_input_tokens": 5, "peak_context": 100,
            "thinking_chars": tc, "text_chars": xc, "tool_calls": 1}


def write_ledger(path, events):
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "metrics.jsonl")
        # a: trivial/fast, est 4 min, actual 6 min (ratio 1.5), worth 30
        # b: small/think, est 8, actual 4 (ratio 0.5), worth 90
        # c: unrated (pending), est 10
        # d: rated but no stats (stats null)
        events = [
            spawn_event("a", complexity="trivial", profile="fast", est_minutes=4),
            spawn_event("b", complexity="small", profile="think", est_minutes=8),
            spawn_event("c", complexity="medium", profile="fast", est_minutes=10),
            spawn_event("d", complexity="small", profile="fast", est_minutes=5),
            rate_event("d", quality=50, worth_it=80),
            rate_event("a", quality=70, worth_it=30, stats=stats(360, out=1000)),
            # a second rate for a: later rate wins
            rate_event("a", quality=60, worth_it=25, stats=stats(360, out=1000)),
            rate_event("b", quality=95, worth_it=90, stats=stats(240, out=2000, tc=300, xc=100)),
        ]
        write_ledger(self.path, events)

    def tearDown(self):
        self.tmp.cleanup()

    def test_join_and_latest_rate_wins(self):
        runs = dr.load_ledger(self.path)
        self.assertEqual(len(runs), 4)
        # later rate wins for run a
        self.assertEqual(runs["a"]["rate"]["worth_it"], 25)
        self.assertEqual(runs["a"]["rate"]["quality"], 60)
        self.assertNotIn("rate", runs["c"])

    def test_table_ratio_math(self):
        runs = dr.load_ledger(self.path)
        rows = { (r["complexity"], r["profile"]): r
                 for r in dr.table_rows(runs) }
        a = rows[("trivial", "fast")]
        self.assertEqual(a["n"], 1)
        self.assertEqual(a["rated"], 1)
        self.assertEqual(a["p50_est_minutes"], 4)
        self.assertEqual(a["p50_actual_minutes"], 6.0)          # 360 s / 60
        self.assertAlmostEqual(a["p50_ratio"], 1.5)            # 6 / 4
        b = rows[("small", "think")]
        self.assertAlmostEqual(b["p50_ratio"], 0.5)            # 4 / 8
        # thinking share: 300 / (300+100) = 0.75
        self.assertAlmostEqual(b["p50_thinking_share"], 0.75)
        # d: rated but stats null -> counted rated, no actual
        d = rows[("small", "fast")]
        self.assertEqual(d["rated"], 1)
        self.assertIsNone(d["p50_actual_minutes"])

    def test_lowest_worth_ordering(self):
        runs = dr.load_ledger(self.path)
        low = dr.lowest_worth(runs, limit=10)
        self.assertEqual([r["run_id"] for r in low], ["a", "d", "b"])
        self.assertEqual(low[0]["worth_it"], 25)
        self.assertEqual(low[0]["minutes"], 6.0)

    def test_missing_ledger(self):
        self.assertIsNone(dr.load_ledger(os.path.join(self.tmp.name, "nope.jsonl")))


class ScheduleTest(unittest.TestCase):
    """4-task DAG with pool=2:
        A(10) -> C(5)
        A(10) -> D(10)
        B(5)  -> C(5)
    (A blocks C and D; B blocks C).
    Greedy longest-est-first, 2 slots:
        t=0: start A(10), B(5)
        t=5: B done, nothing new ready -> slot idle
        t=10: A done -> C ready (10) and D ready (10); start both
        t=20: C, D done  -> makespan 20.
    Critical path: A -> D (20) vs A -> C (15) vs B -> C (10) => 20 via A, D.
    """

    def test_makespan_and_critical_path(self):
        tasks = {"A": 10, "B": 5, "C": 5, "D": 10}
        preds = {"A": set(), "B": set(), "C": {"A", "B"}, "D": {"A"}}
        res = dr.list_schedule(tasks, preds, pool=2)
        self.assertIsNotNone(res)
        self.assertEqual(res["makespan"], 20)
        self.assertEqual(res["schedules"]["A"], (0, 10))
        self.assertEqual(res["schedules"]["B"], (0, 5))
        self.assertEqual(res["schedules"]["C"], (10, 15))
        self.assertEqual(res["schedules"]["D"], (10, 20))
        self.assertEqual(res["critical_path"], ["A", "D"])
        # preds must not be mutated
        self.assertEqual(preds["C"], {"A", "B"})

    def test_blocks_means_successors(self):
        plan = [{"key": "A", "est_minutes": 10, "blocks": ["C", "D"]},
                {"key": "B", "est_minutes": 5, "blocks": ["C"]},
                {"key": "C", "est_minutes": 5, "blocks": []},
                {"key": "D", "est_minutes": 10, "blocks": []}]
        tasks, preds, _ = dr.build_schedule({}, plan, 2)
        self.assertEqual(preds, {"A": set(), "B": set(), "C": {"A", "B"}, "D": {"A"}})
        self.assertEqual(dr.list_schedule(tasks, preds, pool=2)["makespan"], 20)

    def test_cycle_detection(self):
        preds = {"A": {"B"}, "B": {"C"}, "C": {"A"}, "D": set()}
        cyc = dr.find_cycle(preds)
        self.assertIsNotNone(cyc)
        self.assertEqual(len(set(cyc)), len(cyc))  # no repeated key except maybe closure
        self.assertIn("A", cyc)
        # list_schedule gives up (returns None) on the unschedulable tasks
        res = dr.list_schedule({"A": 1, "B": 1, "C": 1, "D": 1},
                               {k: set(v) for k, v in preds.items()}, pool=3)
        self.assertIsNone(res)

    def test_estimate_fallback(self):
        p50 = {"medium": 12}
        self.assertEqual(dr.estimate_minutes({"est_minutes": 4}, p50), 4)
        self.assertEqual(dr.estimate_minutes(
            {"est_minutes": None, "complexity": "medium"}, p50), 12)
        self.assertEqual(dr.estimate_minutes(
            {"est_minutes": None, "complexity": "large"}, p50), 15)

    def test_schedule_includes_pending_runs_and_plan(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "metrics.jsonl")
            # one rated run (excluded), one pending run (included, est 10)
            write_ledger(path, [
                spawn_event("p1", complexity="trivial", est_minutes=3),
                rate_event("p1", quality=80, worth_it=80, stats=stats(300)),
                spawn_event("p2", complexity="trivial", est_minutes=10),
            ])
            plan_path = os.path.join(tmp.name, "plan.json")
            with open(plan_path, "w", encoding="utf-8") as fh:
                json.dump([{"key": "x1", "est_minutes": 2, "blocks": []}], fh)
            runs = dr.load_ledger(path)
            tasks, preds, _labels = dr.build_schedule(runs, [
                {"key": "x1", "est_minutes": 2, "blocks": []}], 3)
            self.assertIn("t-p2", tasks)  # keyed by task_key, else name, else run_id
            self.assertNotIn("p1", tasks)   # rated -> excluded
            self.assertIn("x1", tasks)
            self.assertEqual(tasks["t-p2"], 10)
            self.assertEqual(dr.find_cycle(preds), None)
        finally:
            tmp.cleanup()


class HistoryStatsDedupeTest(unittest.TestCase):
    """3 assistant entries sharing one message.id -> 1 turn, usage once."""

    def test_dedupe_by_message_id(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "s.jsonl")
            usage = {"input_tokens": 100, "cache_read_input_tokens": 50,
                     "cache_creation_input_tokens": 0, "output_tokens": 40}
            entries = [
                {"type": "user", "timestamp": "2026-01-01T00:00:00Z"},
                {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z",
                 "message": {"id": "m1", "usage": usage,
                             "content": [{"type": "text", "text": "hi"}]}},
                # same message.id, same usage repeated
                {"type": "assistant", "timestamp": "2026-01-01T00:00:02Z",
                 "message": {"id": "m1", "usage": usage,
                             "content": [{"type": "text", "text": "hello"}]}},
                {"type": "assistant", "timestamp": "2026-01-01T00:00:03Z",
                 "message": {"id": "m1", "usage": usage,
                             "content": [{"type": "text", "text": "world"}]}},
                # no id -> counts individually
                {"type": "assistant", "timestamp": "2026-01-01T00:00:04Z",
                 "message": {"usage": {"input_tokens": 1, "output_tokens": 2},
                             "content": []}},
            ]
            with open(path, "w", encoding="utf-8") as fh:
                for e in entries:
                    fh.write(json.dumps(e) + "\n")
            m = hs.stream_transcript(path, 0)
            self.assertEqual(m["turns"], 2)          # m1 + one anonymous
            self.assertEqual(m["output_tokens"], 42)  # 40 counted once + 2
            self.assertEqual(m["input_tokens"], 101)  # 100 + 1
            self.assertEqual(m["cache_read_input_tokens"], 50)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()