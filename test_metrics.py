"""Tests for metrics.py (ledger + transcript stats) and the rate_delegate handler."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import metrics
import server


def _assistant(mid, ts, content, usage):
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"id": mid, "content": content, "usage": usage},
    }


def _write_transcript(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        for ln in lines:
            fh.write(json.dumps(ln) + "\n")


def _synthetic_lines():
    """One API response (id "1") split across 3 entries that each repeat the same
    usage (must count once) + a second response (id "2")."""
    u1 = {"input_tokens": 10, "cache_read_input_tokens": 2,
          "cache_creation_input_tokens": 3, "output_tokens": 10}
    u2 = {"input_tokens": 5, "cache_read_input_tokens": 0,
          "cache_creation_input_tokens": 0, "output_tokens": 7}
    return [
        # id "1", first chunk: thinking + text, usage u1
        _assistant("1", "2024-01-01T00:00:00.000Z",
                   [{"type": "thinking", "thinking": "a" * 100},
                    {"type": "text", "text": "b" * 50}],
                   u1),
        # id "1", second chunk: tool_use (id "t1"), usage u1 repeated (dedupe)
        _assistant("1", "2024-01-01T00:00:10Z",
                   [{"type": "tool_use", "id": "t1", "name": "Bash"}],
                   u1),
        # id "1", third chunk: repeats the SAME tool_use block id (dedupe), usage u1
        _assistant("1", "2024-01-01T00:00:20Z",
                   [{"type": "tool_use", "id": "t1", "name": "Bash"}],
                   u1),
        # id "2": single entry, usage u2, a second distinct tool_use (id "t2")
        _assistant("2", "2024-01-01T00:00:30Z",
                   [{"type": "text", "text": "c" * 10},
                    {"type": "tool_use", "id": "t2", "name": "Read"}],
                   u2),
    ]


class TranscriptStatsTests(unittest.TestCase):
    def _stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "s.jsonl")
            _write_transcript(p, _synthetic_lines())
            return metrics.transcript_stats(p)

    def test_usage_deduped_by_message_id(self):
        s = self._stats()
        # Two unique assistant message ids -> two API calls.
        self.assertEqual(s["api_calls"], 2)
        # id "1" usage counted once (10+2+3=15 total input) + id "2" (5) = 20 total input.
        self.assertEqual(s["total_input_tokens"], 20)
        # output: 10 (id 1, once) + 7 (id 2) = 17, not 10*3+7.
        self.assertEqual(s["output_tokens"], 17)
        # cached input = cache_read summed per id: 2 (id1) + 0 (id2) = 2.
        self.assertEqual(s["cached_input_tokens"], 2)
        # first call (id 1, first-seen) total input = 15.
        self.assertEqual(s["first_call_input_tokens"], 15)
        # peak per-id total input = max(15, 5) = 15.
        self.assertEqual(s["peak_context"], 15)

    def test_content_and_tool_and_duration(self):
        s = self._stats()
        # thinking_chars from the one thinking block = 100.
        self.assertEqual(s["thinking_chars"], 100)
        # text chars: 50 (id1 entry1) + 10 (id2) = 60.
        self.assertEqual(s["text_chars"], 60)
        # tool_use deduped by block id: t1 (repeated) + t2 = 2 unique.
        self.assertEqual(s["tool_calls"], 2)
        # duration = last ts - first ts = 30s.
        self.assertEqual(s["duration_s"], 30.0)


class ProfileForTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ("trivial", None, "fast"),
            ("small", None, "fast"),
            ("medium", None, "think"),
            ("large", None, "think"),
            (None, None, "think"),
            ("trivial", "think", "think"),   # explicit wins
            ("large", "fast", "fast"),        # explicit wins
            ("medium", "think", "think"),
            ("unknown", None, "think"),       # unknown complexity -> think
        ]
        for complexity, explicit, expected in cases:
            with self.subTest(complexity=complexity, explicit=explicit):
                self.assertEqual(metrics.profile_for(complexity, explicit), expected)


class AppendEventTests(unittest.TestCase):
    def test_writes_valid_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            metrics.append_event(tmp, {"event": "spawn", "run_id": "abc"})
            metrics.append_event(tmp, {"event": "rate", "run_id": "abc", "stats": None})
            p = os.path.join(tmp, "metrics.jsonl")
            self.assertTrue(os.path.isfile(p))
            with open(p) as fh:
                lines = [json.loads(l) for l in fh if l.strip()]
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0]["event"], "spawn")
            self.assertIsInstance(lines[0]["at"], float)
            self.assertIsNone(lines[1]["stats"])

    def test_best_effort_never_raises(self):
        # A path that cannot be opened must not raise (spawn must not break).
        metrics.append_event("/nonexistent/dir/xyz", {"event": "spawn"})


class RateDelegateTests(unittest.TestCase):
    def _call(self, args):
        return server.rate_delegate(args)

    def test_rejects_quality_out_of_range(self):
        res = self._call({"run_id": "abc", "quality": 101, "worth_it": 50})
        self.assertTrue(res["isError"])
        self.assertIn("quality", res["content"][0]["text"])

    def test_rejects_worth_it_out_of_range(self):
        res = self._call({"run_id": "abc", "quality": 50, "worth_it": -1})
        self.assertTrue(res["isError"])
        self.assertIn("worth_it", res["content"][0]["text"])

    def test_rejects_non_integer(self):
        res = self._call({"run_id": "abc", "quality": "high", "worth_it": 50})
        self.assertTrue(res["isError"])

    def test_rejects_missing_run_id(self):
        res = self._call({"quality": 50, "worth_it": 50})
        self.assertTrue(res["isError"])

    def test_rejects_bool(self):
        # bool is an int subclass; quality=True must not pass range validation.
        res = self._call({"run_id": "abc", "quality": True, "worth_it": 50})
        self.assertTrue(res["isError"])

    def test_records_event_with_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            tr = os.path.join(tmp, "s.jsonl")
            _write_transcript(tr, _synthetic_lines())
            with patch.object(server, "STATE_DIR", tmp), \
                 patch.object(server, "_resolve_agent",
                              return_value=({"sessionId": "xyz"}, None)), \
                 patch.object(server, "_find_transcript", return_value=tr):
                res = self._call({"run_id": "abc", "quality": 80, "worth_it": 90,
                                  "note": "x" * 600})
            self.assertFalse(res["isError"])
            p = os.path.join(tmp, "metrics.jsonl")
            with open(p) as fh:
                lines = [json.loads(l) for l in fh if l.strip()]
            rate = next(e for e in lines if e["event"] == "rate")
            self.assertEqual(rate["quality"], 80)
            self.assertEqual(rate["worth_it"], 90)
            # note truncated to 500
            self.assertEqual(len(rate["note"]), 500)
            self.assertEqual(rate["stats"]["api_calls"], 2)
            # confirmation mentions duration and output tokens
            text = res["content"][0]["text"]
            self.assertIn("30.0", text)
            self.assertIn("17", text)

    def test_records_event_when_transcript_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(server, "STATE_DIR", tmp), \
                 patch.object(server, "_resolve_agent",
                              return_value=({"sessionId": "none"}, None)), \
                 patch.object(server, "_find_transcript", return_value=None):
                res = self._call({"run_id": "abc", "quality": 10, "worth_it": 5})
            self.assertFalse(res["isError"])
            with open(os.path.join(tmp, "metrics.jsonl")) as fh:
                lines = [json.loads(l) for l in fh if l.strip()]
            rate = next(e for e in lines if e["event"] == "rate")
            self.assertIsNone(rate["stats"])
            self.assertIn("no transcript", res["content"][0]["text"])


class SpawnModelRoutingTests(unittest.TestCase):
    def _spawn(self, tmp, **spawn_kwargs):
        settings = Path(tmp) / "local.json"
        settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://localhost:4000",
                                                 "ANTHROPIC_MODEL": "local-test",
                                                 "ANTHROPIC_API_KEY": "test-only"}}))
        rid, err = server._spawn_native_agent(
            "hello", "Read", tmp, "probe",
            **spawn_kwargs,
        )
        return rid, err

    def test_fast_model_overrides_env_and_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(server, "_default_settings_path",
                              return_value=str(Path(tmp) / "local.json")), \
                 patch.object(server, "STATE_DIR", tmp), \
                 patch.object(server, "FAST_MODEL", "cheap-model"), \
                 patch.object(server.subprocess, "run") as run:
                run.return_value.stdout = b"backgrounded abc12345"
                run.return_value.returncode = 0
                rid, err = self._spawn(tmp, profile="fast", spawn_model="cheap-model")
            self.assertIsNone(err)
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[cmd.index("--model") + 1], "cheap-model")
            env = run.call_args.kwargs["env"]
            for var in ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL",
                        "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                        "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
                self.assertEqual(env[var], "cheap-model")

    def test_default_profile_keeps_backend_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(server, "_default_settings_path",
                              return_value=str(Path(tmp) / "local.json")), \
                 patch.object(server, "STATE_DIR", tmp), \
                 patch.object(server.subprocess, "run") as run:
                run.return_value.stdout = b"backgrounded abc12345"
                run.return_value.returncode = 0
                rid, err = self._spawn(tmp, profile="think", spawn_model=None)
            self.assertIsNone(err)
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[cmd.index("--model") + 1], "local-test")


class SpawnLedgerEventTests(unittest.TestCase):
    def test_spawn_appends_ledger_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "local.json"
            settings.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "http://localhost:4000",
                                                     "ANTHROPIC_MODEL": "local-test",
                                                     "ANTHROPIC_API_KEY": "test-only"}}))
            with patch.object(server, "_default_settings_path",
                              return_value=str(settings)), \
                 patch.object(server, "STATE_DIR", tmp), \
                 patch.object(server.subprocess, "run") as run:
                run.return_value.stdout = b"backgrounded abc12345"
                run.return_value.returncode = 0
                server._spawn_native_agent(
                    "hello", "Read", tmp, "probe",
                    profile="think", spawn_model=None,
                    complexity="large", est_minutes=5,
                    blocks=["t1", "t2"], task_id="TASK123",
                )
            with open(os.path.join(tmp, "metrics.jsonl")) as fh:
                lines = [json.loads(l) for l in fh if l.strip()]
            spawn = next(e for e in lines if e["event"] == "spawn")
            self.assertEqual(spawn["run_id"], "abc12345")
            self.assertEqual(spawn["task_id"], "TASK123")
            self.assertEqual(spawn["complexity"], "large")
            self.assertEqual(spawn["est_minutes"], 5)
            self.assertEqual(spawn["blocks"], ["t1", "t2"])
            self.assertEqual(spawn["profile"], "think")
            self.assertEqual(spawn["model"], "local-test")
            self.assertEqual(spawn["prompt_chars"], len("hello"))


class SpawnSignatureTests(unittest.TestCase):
    def test_new_kwargs_default_none_and_old_callers_work(self):
        # The existing 8/9-positional-arg call sites must keep working: new args
        # default to None so fan_out / verified / continue_delegate are unchanged.
        import inspect
        sig = inspect.signature(server._spawn_native_agent)
        params = list(sig.parameters)
        for p in ("profile", "spawn_model", "complexity", "est_minutes",
                  "blocks", "task_id"):
            self.assertIn(p, params)
            self.assertIsNone(sig.parameters[p].default)


if __name__ == "__main__":
    unittest.main()