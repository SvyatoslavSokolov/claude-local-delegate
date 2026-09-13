"""Focused tests for search-churn observability in contrib/history_stats.py."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "contrib"))

import history_stats as hs  # noqa: E402


def tool_entry(ts, name):
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": "m-" + ts, "usage": {"input_tokens": 1, "output_tokens": 1},
                        "content": [{"type": "tool_use", "name": name}]}}


class SearchCallsPerSessionTest(unittest.TestCase):
    def test_counts_grep_and_glob_only(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "s.jsonl")
            entries = [tool_entry("2026-01-01T00:00:0%dZ" % i, n)
                       for i, n in enumerate(["Grep", "Glob", "Read", "Bash", "Grep"])]
            with open(path, "w", encoding="utf-8") as fh:
                for e in entries:
                    fh.write(json.dumps(e) + "\n")
            m = hs.stream_transcript(path, 0)
            self.assertEqual(m["search_calls"], 3)   # Grep x2 + Glob
            self.assertEqual(m["tool_calls"], 5)
        finally:
            tmp.cleanup()

    def test_zero_when_no_search_tools(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "s.jsonl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(tool_entry("2026-01-01T00:00:00Z", "Read")) + "\n")
            m = hs.stream_transcript(path, 0)
            self.assertEqual(m["search_calls"], 0)
        finally:
            tmp.cleanup()


def make_metric(search_calls, tool_calls):
    m = {k: 0 for k in ("duration_seconds", "turns", "input_tokens",
                        "cache_read_input_tokens", "cache_creation_input_tokens",
                        "output_tokens", "total_input_tokens", "peak_context_tokens",
                        "final_chars", "final_lines")}
    m.update({"search_calls": search_calls, "tool_calls": tool_calls,
              "tools": {}})
    return m


class SummarizeSearchChurnTest(unittest.TestCase):
    def test_thresholds_and_distribution(self):
        metrics = [make_metric(s, tc) for s, tc in
                   [(0, 10), (5, 20), (11, 30), (31, 41), (7, 40)]]
        from collections import Counter
        out = hs.summarize(metrics, Counter())
        th = out["thresholds"]
        self.assertEqual(th["search_calls"]["gt_10"]["count"], 2)      # 11, 31
        self.assertEqual(th["search_calls"]["gt_30"]["count"], 1)      # 31
        self.assertEqual(th["tool_calls_gt_40"]["gt_40"]["count"], 1)  # 41 only
        self.assertAlmostEqual(th["search_calls"]["gt_10"]["percent"], 40.0)
        dist = out["search_churn"]["per_session_distribution"]
        self.assertEqual(dist, {0: 1, 5: 1, 7: 1, 11: 1, 31: 1})
        self.assertEqual(out["search_churn"]["total_search_calls"], 54)
        # percentile block exists and is deterministic
        self.assertIn("search_calls", out["percentiles"])
        self.assertEqual(out["percentiles"]["search_calls"]["max"], 31)

    def test_empty_input(self):
        from collections import Counter
        out = hs.summarize([], Counter())
        self.assertEqual(out["thresholds"]["search_calls"]["gt_10"]["count"], 0)
        self.assertEqual(out["thresholds"]["search_calls"]["gt_10"]["percent"], 0.0)
        self.assertEqual(out["search_churn"]["per_session_distribution"], {})
        self.assertEqual(out["search_churn"]["total_search_calls"], 0)
        self.assertIsNone(out["percentiles"]["search_calls"]["mean"])


if __name__ == "__main__":
    unittest.main()