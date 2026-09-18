"""Focused tests for the runaway-turn watchdog (server._turn_guard_check).

The forensic audit found local delegation runs drifting to 41-61 model turns
(45-64 tool calls) with ZERO long tool-free runs. The invoked `claude --bg` CLI
has no `--max-turns` / `--max-tool-calls` flag, so the cap is enforced
supervisor-side by watching the transcript's model-turn count and SIGTERM-ing a
run that exceeds it. These tests prove the launch/control path applies the
ceiling, the env override is validated/used, and the allowed tool set is
untouched. No model is invoked and no real process is signalled (os.kill is
patched).
"""
import importlib.util
import json
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))


def load_server(tmp, max_turns_env=None):
    """Import a fresh server module with state pointed at a temp dir.

    ``max_turns_env`` (str or None) sets CLAUDE_LOCAL_DELEGATE_MAX_TURNS for the
    import; None leaves the variable unset so the built-in default is used.
    """
    env = {'CLAUDE_LOCAL_DELEGATE_STATE_DIR': tmp}
    if max_turns_env is None:
        env.pop('CLAUDE_LOCAL_DELEGATE_MAX_TURNS', None)
    else:
        env['CLAUDE_LOCAL_DELEGATE_MAX_TURNS'] = max_turns_env
    with patch.dict(os.environ, env, clear=False):
        # Drop any cached module so the env is re-read at import time.
        for name in ('turn_guard_server',):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location('turn_guard_server', ROOT / 'server.py')
        s = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(s)
    s.STATE_DIR = tmp
    s.PROVENANCE_PATH = os.path.join(tmp, 'runs.json')
    s.VERIFIED_DIR = os.path.join(tmp, 'verified')
    s.BATCHES_DIR = os.path.join(tmp, 'batches')
    return s


def _write_transcript(path, n_assistant):
    """Write a JSONL transcript with exactly ``n_assistant`` assistant events.

    ``_transcript_summary`` counts one model turn per assistant event, so this
    is the deterministic stand-in for a run that has made N turns."""
    lines = []
    for i in range(n_assistant):
        lines.append(json.dumps({
            "type": "assistant",
            "message": {"id": f"msg-{i}", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": f"step {i}"}],
                        "usage": {"output_tokens": 10}},
        }))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


class DefaultCeilingTest(unittest.TestCase):
    def test_default_is_conservative_40(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_server(tmp).DEFAULT_MAX_TURNS, 40)

    def test_override_is_read_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_server(tmp, "55").DEFAULT_MAX_TURNS, 55)

    def test_zero_disables(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_server(tmp, "0").DEFAULT_MAX_TURNS, 0)

    def test_garbage_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_server(tmp, "not-an-int").DEFAULT_MAX_TURNS, 40)

    def test_negative_is_clamped_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_server(tmp, "-3").DEFAULT_MAX_TURNS, 40)


class GuardTriggerTest(unittest.TestCase):
    """The control path stops a run once its turn count EXCEEDS the ceiling."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = load_server(self.tmp.name)  # default cap 40
        self.transcript = _write_transcript(
            os.path.join(self.tmp.name, 's.jsonl'), 41)  # 41 > 40

    def _agent(self, state='working'):
        return {'id': 'aa11bb22', 'sessionId': 'sess-1',
                'cwd': self.tmp.name, 'state': state, 'pid': 999999}

    def test_check_status_kills_run_past_cap_and_reports_distinct_reason(self):
        kill = MagicMock()
        with patch.object(self.s, '_resolve_agent', return_value=(self._agent(), None)), \
             patch.object(self.s, '_find_transcript', return_value=self.transcript), \
             patch.object(self.s.os, 'kill', kill):
            result = self.s.check_status({'run_id': 'aa11bb22'})
        text = result['content'][0]['text']
        self.assertFalse(result['isError'])
        self.assertIn('TURN GUARD', text)
        self.assertIn('SIGTERM', text)
        self.assertIn('NOT a user stop_delegate', text)
        kill.assert_called_once_with(999999, signal.SIGTERM)
        # The guard is reported once, not re-reported on a second poll.
        with patch.object(self.s, '_resolve_agent', return_value=(self._agent(), None)), \
             patch.object(self.s, '_find_transcript', return_value=self.transcript), \
             patch.object(self.s.os, 'kill', kill):
            result2 = self.s.check_status({'run_id': 'aa11bb22'})
        self.assertNotIn('TURN GUARD', result2['content'][0]['text'])
        self.assertEqual(kill.call_count, 1)  # no second signal

    def test_check_status_records_guard_event_in_ledger(self):
        with patch.object(self.s, '_resolve_agent', return_value=(self._agent(), None)), \
             patch.object(self.s, '_find_transcript', return_value=self.transcript), \
             patch.object(self.s.os, 'kill', MagicMock()):
            self.s.check_status({'run_id': 'aa11bb22'})
        events = [json.loads(l) for l in
                  open(os.path.join(self.tmp.name, 'metrics.jsonl')) if l.strip()]
        guard = [e for e in events if e.get('event') == 'turn-guard']
        self.assertEqual(len(guard), 1)
        self.assertEqual(guard[0]['run_id'], 'aa11bb22')
        self.assertEqual(guard[0]['turns'], 41)
        self.assertEqual(guard[0]['cap'], 40)

    def test_at_cap_is_not_killed_only_beyond(self):
        self.transcript = _write_transcript(os.path.join(self.tmp.name, 's.jsonl'), 40)
        kill = MagicMock()
        with patch.object(self.s, '_resolve_agent', return_value=(self._agent(), None)), \
             patch.object(self.s, '_find_transcript', return_value=self.transcript), \
             patch.object(self.s.os, 'kill', kill):
            result = self.s.check_status({'run_id': 'aa11bb22'})
        self.assertNotIn('TURN GUARD', result['content'][0]['text'])
        kill.assert_not_called()

    def test_ordinary_short_run_is_untouched(self):
        self.transcript = _write_transcript(os.path.join(self.tmp.name, 's.jsonl'), 5)
        kill = MagicMock()
        with patch.object(self.s, '_resolve_agent', return_value=(self._agent(), None)), \
             patch.object(self.s, '_find_transcript', return_value=self.transcript), \
             patch.object(self.s.os, 'kill', kill):
            result = self.s.check_status({'run_id': 'aa11bb22'})
        self.assertNotIn('TURN GUARD', result['content'][0]['text'])
        kill.assert_not_called()

    def test_settled_run_is_untouched(self):
        kill = MagicMock()
        with patch.object(self.s, '_resolve_agent',
                          return_value=(self._agent(state='completed'), None)), \
             patch.object(self.s, '_find_transcript', return_value=self.transcript), \
             patch.object(self.s.os, 'kill', kill):
            result = self.s.check_status({'run_id': 'aa11bb22'})
        self.assertNotIn('TURN GUARD', result['content'][0]['text'])
        kill.assert_not_called()

    def test_disabled_cap_never_kills(self):
        self.s.DEFAULT_MAX_TURNS = 0  # CLAUDE_LOCAL_DELEGATE_MAX_TURNS=0
        kill = MagicMock()
        with patch.object(self.s, '_resolve_agent', return_value=(self._agent(), None)), \
             patch.object(self.s, '_find_transcript', return_value=self.transcript), \
             patch.object(self.s.os, 'kill', kill):
            result = self.s.check_status({'run_id': 'aa11bb22'})
        self.assertNotIn('TURN GUARD', result['content'][0]['text'])
        kill.assert_not_called()

    def test_get_result_reports_guard_termination_not_a_final_answer(self):
        kill = MagicMock()
        with patch.object(self.s, '_resolve_agent', return_value=(self._agent(), None)), \
             patch.object(self.s, '_find_transcript', return_value=self.transcript), \
             patch.object(self.s.os, 'kill', kill):
            result = self.s.get_result({'run_id': 'aa11bb22'})
        text = result['content'][0]['text']
        self.assertIn('TURN GUARD', text)
        self.assertIn('SIGTERM', text)
        kill.assert_called_once_with(999999, signal.SIGTERM)

    def test_custom_env_override_is_used_by_the_guard(self):
        # A run of 51 turns is below the default 40? no -- below a raised 55.
        self.transcript = _write_transcript(os.path.join(self.tmp.name, 's.jsonl'), 51)
        kill = MagicMock()
        # Under the default cap (40) this would be killed...
        with patch.object(self.s, '_resolve_agent', return_value=(self._agent(), None)), \
             patch.object(self.s, '_find_transcript', return_value=self.transcript), \
             patch.object(self.s.os, 'kill', kill):
            self.s.check_status({'run_id': 'aa11bb22'})
        self.assertEqual(kill.call_count, 1)  # killed under cap 40
        self.s._TURN_GUARD_KILLED.clear()
        # ...but a raised ceiling (55) must let it run: proves the override is used.
        self.s.DEFAULT_MAX_TURNS = 55
        kill2 = MagicMock()
        with patch.object(self.s, '_resolve_agent', return_value=(self._agent(), None)), \
             patch.object(self.s, '_find_transcript', return_value=self.transcript), \
             patch.object(self.s.os, 'kill', kill2):
            result = self.s.check_status({'run_id': 'aa11bb22'})
        self.assertNotIn('TURN GUARD', result['content'][0]['text'])
        kill2.assert_not_called()


class LaunchPathTest(unittest.TestCase):
    """The launch path records the ceiling and leaves the tool set unchanged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = load_server(self.tmp.name)
        self.settings = Path(self.tmp.name) / 'vllm.json'
        self.settings.write_text(json.dumps({'env': {
            'ANTHROPIC_BASE_URL': 'http://127.0.0.1:4000',
            'ANTHROPIC_MODEL': 'local-qwen', 'ANTHROPIC_API_KEY': 'x'}}))

    def _spawn(self, allowed_tools=None):
        calls = {}

        class Proc:
            returncode = 0
            stdout = b'backgrounded  aa46976f'

        def fake_run(cmd, **kwargs):
            calls['cmd'] = cmd
            return Proc()

        with patch.object(self.s, '_default_settings_path', lambda: str(self.settings)), \
             patch.object(self.s.subprocess, 'run', fake_run):
            run_id, err = self.s._spawn_native_agent(
                'task', allowed_tools, self.tmp.name, 'n')
        self.assertIsNone(err, err)
        return run_id, calls['cmd']

    def test_spawn_records_the_turn_ceiling_in_provenance(self):
        run_id, _cmd = self._spawn(None)
        prov = self.s._provenance_for(run_id)
        self.assertEqual(prov['max_turns'], 40)

    def test_allowed_tools_unchanged_by_guard(self):
        # The guard is supervisor-side and must NOT shrink what the agent can do.
        # The default writer set is present in the launch command exactly as before.
        _run_id, cmd = self._spawn(None)
        i = cmd.index('--tools')
        tools = {t for t in cmd[i + 1].split(',') if t}
        expected = {'Read', 'Grep', 'Glob', 'Edit', 'Write', 'Bash',
                    'WebSearch', 'WebFetch', 'LSP', 'NotebookRead', 'NotebookEdit',
                    'Skill', 'SendMessage', 'ListAgents', 'TodoWrite', 'ReportFindings',
                    'ScheduleWakeup', 'WaitForMcpServers', 'EnterWorktree', 'ExitWorktree',
                    'CronCreate', 'CronDelete', 'CronList'}
        self.assertEqual(tools, expected)
        allowed = set(cmd[cmd.index('--allowedTools') + 1:])
        self.assertTrue(expected <= allowed)
        # No turn-cap flag was invented: the launch command has no max-turns.
        self.assertFalse(any(a.startswith('--max-turn') or a == '--max-turns'
                             for a in cmd))

    def test_spawn_records_custom_ceiling(self):
        self.s.DEFAULT_MAX_TURNS = 55
        run_id, _cmd = self._spawn(None)
        self.assertEqual(self.s._provenance_for(run_id)['max_turns'], 55)


if __name__ == '__main__':
    unittest.main()