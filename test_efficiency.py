"""Regressions for the token/latency work: compaction, single-pass transcript
reading, enforced read-only privilege, routing provenance, conditional verify."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load_server(tmp):
    spec = importlib.util.spec_from_file_location('eff_server', Path(__file__).with_name('server.py'))
    s = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s)
    s.STATE_DIR = tmp
    s.PROVENANCE_PATH = os.path.join(tmp, 'runs.json')
    s.VERIFIED_DIR = os.path.join(tmp, 'verified')
    s.BATCHES_DIR = os.path.join(tmp, 'batches')
    return s


def transcript(path, assistant_texts, prompt='do the thing'):
    events = [{'type': 'user', 'message': {'content': [{'type': 'text', 'text': prompt}]}}]
    for text in assistant_texts:
        events.append({'type': 'assistant', 'message': {
            'content': [{'type': 'text', 'text': text}],
            'usage': {'input_tokens': 10, 'cache_read_input_tokens': 5, 'output_tokens': 7}}})
    Path(path).write_text(''.join(json.dumps(e) + '\n' for e in events))
    return path


class EfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = load_server(self.tmp.name)

    def test_parent_tool_schema_stays_compact(self):
        encoded = json.dumps(self.s.PARENT_TOOLS, separators=(',', ':'))
        self.assertLess(len(encoded), 12000)

    # ---- compaction ---------------------------------------------------------
    def test_compact_keeps_short_text_whole(self):
        body, trimmed = self.s._compact('one\ntwo', 40, 'compacted', 'ask for full')
        self.assertFalse(trimmed)
        self.assertEqual(body, 'one\ntwo')

    def test_compact_trims_and_stays_auditable(self):
        text = '\n'.join(f'line {i}' for i in range(500))
        body, trimmed = self.s._compact(text, 40, 'compacted', 'ask for full')
        self.assertTrue(trimmed)
        self.assertIn('line 499', body)          # the conclusion survives
        self.assertNotIn('line 200', body)       # the middle does not
        self.assertIn('sha256:', body)           # the elision is checkable
        self.assertLess(len(body), len(text))

    def test_compact_does_not_frame_a_text_it_cannot_shrink(self):
        text = "\n".join(f"line {i}" for i in range(42))
        body, trimmed = self.s._compact(text, 40, "compacted", "ask for full")
        self.assertFalse(trimmed)
        self.assertEqual(body, text)

    def test_full_flag_and_max_lines(self):
        self.assertEqual(self.s._result_max_lines({'full': True}, 40), 0)
        self.assertEqual(self.s._result_max_lines({'max_lines': 5}, 40), 5)
        self.assertEqual(self.s._result_max_lines({}, 40), 40)
        self.assertEqual(self.s._result_max_lines({'max_lines': 'junk'}, 40), 40)

    def test_result_wait_seconds_is_bounded(self):
        self.assertEqual(self.s._result_wait_seconds({}), 0)
        self.assertEqual(self.s._result_wait_seconds({'wait_seconds': -1}), 0)
        self.assertEqual(self.s._result_wait_seconds({'wait_seconds': '45'}), 45)
        self.assertEqual(
            self.s._result_wait_seconds({'wait_seconds': 9999}),
            self.s.MAX_RESULT_WAIT_SECONDS,
        )

    def test_get_result_waits_server_side_then_returns_final(self):
        path = transcript(os.path.join(self.tmp.name, 'wait.jsonl'), ['compact final'])
        working = {'id': 'abc12345', 'state': 'working', 'sessionId': 'session-x'}
        done = {'id': 'abc12345', 'state': 'done', 'sessionId': 'session-x'}
        with patch.object(self.s, '_resolve_agent', side_effect=[(working, None), (done, None)]) as resolve, \
             patch.object(self.s, '_find_transcript', return_value=path), \
             patch.object(self.s.time, 'sleep') as sleep:
            result = self.s.get_result({'run_id': 'abc12345', 'wait_seconds': 10})
        self.assertFalse(result['isError'])
        self.assertIn('compact final', result['content'][0]['text'])
        self.assertEqual(resolve.call_count, 2)
        sleep.assert_called_once()

    # ---- one pass, memoised -------------------------------------------------
    def test_transcript_is_read_once_and_cached(self):
        path = transcript(os.path.join(self.tmp.name, 't.jsonl'), ['first', 'second'])
        reads = []
        original = self.s._iter_events

        def counting(p):
            reads.append(p)
            return original(p)

        with patch.object(self.s, '_iter_events', counting):
            summary = self.s._transcript_summary(path)
            self.s._narration_digest(path)
            self.s._token_usage(path)
            self.s._first_user_text(path)
        self.assertEqual(len(reads), 1, 'four supervision reads must cost one pass')
        self.assertEqual(summary['turns'], 2)
        self.assertEqual(summary['output'], 14)
        self.assertEqual(summary['input'], 30)
        self.assertEqual(summary['prompt'], 'do the thing')
        self.assertEqual(summary['digest'], ['[1] first', '[2] second'])

    def test_cache_invalidates_when_the_agent_writes_more(self):
        path = transcript(os.path.join(self.tmp.name, 't.jsonl'), ['first'])
        self.assertEqual(self.s._transcript_summary(path)['turns'], 1)
        os.utime(path, (0, 0))
        transcript(path, ['first', 'second'])
        self.assertEqual(self.s._transcript_summary(path)['turns'], 2)

    # ---- privilege ----------------------------------------------------------
    def test_read_only_allowlist_is_detected(self):
        self.assertTrue(self.s._is_read_only('Read,Grep,Glob'))
        self.assertTrue(self.s._is_read_only('Read'))
        self.assertFalse(self.s._is_read_only('Read,Bash'))
        self.assertFalse(self.s._is_read_only('Read,Write'))

    def _spawn(self, allowed_tools, **kw):
        calls = {}

        class Proc:
            returncode = 0
            stdout = b'backgrounded  aa46976f'

        def fake_run(cmd, **kwargs):
            calls['cmd'] = cmd
            return Proc()

        settings = Path(self.tmp.name) / 'vllm.json'
        settings.write_text(json.dumps({'env': {
            'ANTHROPIC_BASE_URL': 'http://192.168.1.109:4000',
            'ANTHROPIC_MODEL': 'local-qwen', 'ANTHROPIC_API_KEY': 'x'}}))
        with patch.object(self.s, '_default_settings_path', lambda: str(settings)), \
             patch.object(self.s.subprocess, 'run', fake_run):
            run_id, err = self.s._spawn_native_agent('task', allowed_tools, self.tmp.name, 'n', **kw)
        self.assertIsNone(err)
        return run_id, calls['cmd']

    def test_read_only_delegate_gets_an_enforced_allowlist(self):
        # bypassPermissions ignores --allowedTools, so a read-only delegation
        # must not run in it.
        _, cmd = self._spawn('Read,Grep,Glob')
        self.assertEqual(cmd[cmd.index('--permission-mode') + 1], 'dontAsk')

    def test_writer_delegate_still_runs_unattended(self):
        _, cmd = self._spawn('Read,Write,Bash')
        self.assertEqual(cmd[cmd.index('--permission-mode') + 1], 'bypassPermissions')

    def test_explicit_permission_mode_wins(self):
        _, cmd = self._spawn('Read,Grep,Glob', permission_mode='acceptEdits')
        self.assertEqual(cmd[cmd.index('--permission-mode') + 1], 'acceptEdits')

    # ---- provenance ---------------------------------------------------------
    def test_spawn_records_which_backend_ran_it(self):
        run_id, _ = self._spawn('Read,Grep,Glob')
        record = self.s._provenance_for(run_id)
        self.assertEqual(record['backend'], 'local')
        self.assertEqual(record['model'], 'local-qwen')
        self.assertEqual(record['base_url'], 'http://192.168.1.109:4000')
        self.assertTrue(record['read_only'])
        self.assertTrue(record['settings_sha'])
        self.assertIsNone(self.s._provenance_for('deadbeef'))

    def test_provenance_survives_a_corrupt_file(self):
        Path(self.s.PROVENANCE_PATH).write_text('{ not json')
        self.assertEqual(self.s._load_provenance(), {})
        self.s._record_provenance('abc12345', {'at': 1, 'backend': 'local'})
        self.assertEqual(self.s._provenance_for('abc12345')['backend'], 'local')

    # ---- conditional verification ------------------------------------------
    def test_no_checker_for_a_read_only_run_without_criteria(self):
        state = {'acceptance': None, 'allowed_tools': 'Read,Grep,Glob', 'cwd': self.tmp.name}
        self.assertIn('read-only', self.s._skip_verification(state))

    def test_checker_runs_when_criteria_are_stated(self):
        state = {'acceptance': 'tests pass', 'allowed_tools': 'Read,Grep,Glob', 'cwd': self.tmp.name}
        self.assertIsNone(self.s._skip_verification(state))

    def test_always_verify_forces_the_checker(self):
        state = {'acceptance': None, 'allowed_tools': 'Read,Grep,Glob',
                 'cwd': self.tmp.name, 'always_verify': True}
        self.assertIsNone(self.s._skip_verification(state))

    def test_writer_run_outside_git_still_gets_a_checker(self):
        state = {'acceptance': None, 'allowed_tools': 'Read,Write,Bash', 'cwd': self.tmp.name}
        self.assertIsNone(self.s._skip_verification(state))  # cannot prove nothing changed

    def test_clean_git_tree_means_nothing_to_verify(self):
        import subprocess
        for argv in (['init', '-q'], ['add', '-A'], ['-c', 'user.email=t@t', '-c', 'user.name=t',
                                                     'commit', '-qm', 'x', '--allow-empty']):
            subprocess.run(['git', '-C', self.tmp.name, *argv], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        state = {'acceptance': None, 'allowed_tools': 'Read,Write,Bash', 'cwd': self.tmp.name}
        self.assertIn('clean', self.s._skip_verification(state))
        Path(self.tmp.name, 'new.txt').write_text('worker wrote this')
        self.assertIsNone(self.s._skip_verification(state))


class VerifiedLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = load_server(self.tmp.name)
        self.transcript = transcript(os.path.join(self.tmp.name, 'w.jsonl'),
                                     ['I read the files.', 'Here is the answer.'])
        self.s._agent_settled_state = lambda aid: ('completed', 'session-x')
        self.s._find_transcript = lambda sid: self.transcript

    def state(self, **kw):
        base = {'vid': 'a' * 12, 'created_at': self.s.time.time(), 'phase': 'working',
                'spec': 'summarise the module', 'acceptance': None, 'cwd': self.tmp.name,
                'allowed_tools': 'Read,Grep,Glob', 'max_iters': 3, 'timeout': 600,
                'iteration': 1, 'current_worker_id': 'w1', 'current_checker_id': None,
                'history': [], 'final_answer': None, 'failure_report': None,
                'always_verify': False, 'skipped_verification': None}
        base.update(kw)
        return base

    def test_read_only_run_passes_without_spawning_a_checker(self):
        spawned = []
        with patch.object(self.s, '_spawn_verify_checker', lambda *a: spawned.append(a) or ('c1', None)):
            state = self.state()
            line = self.s._advance_verified(state)
        self.assertEqual(spawned, [], 'no checker should be spawned for this run')
        self.assertEqual(state['phase'], 'passed')
        self.assertEqual(state['final_answer'], 'Here is the answer.')
        self.assertIn('WITHOUT a checker', line)
        self.assertIn('read-only', state['skipped_verification'])

    def test_criteria_make_the_checker_run(self):
        spawned = []

        def fake_checker(state, candidate):
            spawned.append(candidate)
            return 'c1', None

        with patch.object(self.s, '_spawn_verify_checker', fake_checker):
            state = self.state(acceptance='the summary names every public function')
            self.s._advance_verified(state)
        self.assertEqual(spawned, ['Here is the answer.'])
        self.assertEqual(state['phase'], 'checking')
        self.assertEqual(state['current_checker_id'], 'c1')

    def test_checker_prompt_carries_git_evidence_not_the_whole_report(self):
        import subprocess
        for argv in (['init', '-q'], ['-c', 'user.email=t@t', '-c', 'user.name=t',
                                      'commit', '-qm', 'x', '--allow-empty']):
            subprocess.run(['git', '-C', self.tmp.name, *argv], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pathlib_path = Path(self.tmp.name, 'touched.py')
        pathlib_path.write_text('x = 1\n')
        captured = {}
        self.s._spawn_native_agent = lambda task, *a, **kw: (captured.setdefault('task', task), ('c1', None))[1]
        huge = '\n'.join(f'self report line {i}' for i in range(400))
        self.s._spawn_verify_checker(self.state(acceptance='it builds'), huge)
        prompt = captured['task']
        self.assertIn('git status --porcelain', prompt)
        self.assertIn('touched.py', prompt)
        self.assertIn('self report line 399', prompt)      # the tail of the claim
        self.assertNotIn('self report line 10\n', prompt)  # not the whole claim
        self.assertLess(len(prompt), len(huge))


if __name__ == '__main__':
    unittest.main()
