"""Deep delegation (deep_dive / context_pack / review_diff / project_findings)
and the engine status reader, without model calls or network to the real engine."""
import http.server
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import deep_tasks
import engine_status


def load_server(state_dir):
    spec = importlib.util.spec_from_file_location(
        'deep_server', Path(__file__).resolve().parent.parent / 'server.py')
    s = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s)
    s.STATE_DIR = state_dir
    s.PROVENANCE_PATH = os.path.join(state_dir, 'runs.json')
    s._TURN_CAPS_FILE = os.path.join(state_dir, 'turn_caps.json')
    s.FINDINGS_DIR = os.path.join(state_dir, 'findings')
    return s


class DeepToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = os.path.join(self.tmp.name, 'proj')
        os.makedirs(self.project)
        self.s = load_server(self.tmp.name)
        self.spawned = []

        def fake_spawn(task, allowed_tools, cwd, name, *a, **kw):
            self.spawned.append({'task': task, 'cwd': cwd, 'name': name, **kw})
            self.s._record_provenance('d1', {'at': 1.0, 'backend': 'local'})
            return 'd1', None
        self.s._spawn_native_agent = fake_spawn

    def test_deep_dive_records_report_path_persona_and_turn_cap(self):
        out = self.s.deep_dive({'question': 'Why does X leak?', 'cwd': self.project,
                                'context': 'suspect the pool'})
        self.assertFalse(out['isError'])
        call = self.spawned[0]
        self.assertEqual(call['agent'], 'local-deep')
        self.assertEqual(call['complexity'], 'large')
        self.assertIn('[DEEP]', call['name'])
        report = self.s._provenance_for('d1')['report_path']
        self.assertTrue(report.startswith(deep_tasks.findings_dir(self.s.FINDINGS_DIR, self.project)))
        self.assertIn(report, call['task'])
        self.assertIn('suspect the pool', call['task'])
        self.assertIn('Do NOT modify project files', call['task'])
        self.assertTrue(os.path.isdir(os.path.dirname(report)))
        self.assertEqual(self.s._run_turn_cap('d1', 40), self.s.DEEP_MAX_TURNS)

    def test_allow_edits_and_explicit_turn_cap(self):
        self.s.deep_dive({'question': 'q', 'cwd': self.project, 'allow_edits': True, 'max_turns': 0})
        self.assertIn('MAY modify project files', self.spawned[0]['task'])
        self.assertEqual(self.s._run_turn_cap('d1', 40), 0)

    def test_review_diff_rejects_non_revision_base(self):
        out = self.s.review_diff({'cwd': self.project, 'base': 'HEAD; rm -rf /'})
        self.assertTrue(out['isError'])
        self.assertEqual(self.spawned, [])
        out = self.s.review_diff({'cwd': self.project, 'base': 'HEAD~2', 'test_command': 'pytest -q'})
        self.assertFalse(out['isError'])
        self.assertIn('git diff HEAD~2', self.spawned[0]['task'])
        self.assertIn('`pytest -q`', self.spawned[0]['task'])

    def test_context_pack_and_required_args(self):
        self.assertTrue(self.s.context_pack({'cwd': self.project})['isError'])
        self.assertTrue(self.s.deep_dive({'question': '  ', 'cwd': self.project})['isError'])
        self.s.context_pack({'task': 'add retries', 'focus': 'net/client.py', 'cwd': self.project})
        self.assertIn('[PACK]', self.spawned[0]['name'])
        self.assertIn('## Tests and checks', self.spawned[0]['task'])

    def test_result_returns_whole_dossier_then_final_message(self):
        self.s.deep_dive({'question': 'q', 'cwd': self.project})
        report = self.s._provenance_for('d1')['report_path']
        body = '# Answer\n' + '\n'.join(f'fact {i}' for i in range(500))
        Path(report).write_text(body)
        out = self.s._with_deep_report('d1', 'final msg')
        self.assertIn('fact 0\n', out)
        self.assertIn('fact 499', out)
        self.assertTrue(out.endswith('=== FINAL MESSAGE ===\nfinal msg'))
        self.assertEqual(self.s._with_deep_report('plain-run', 'x'), 'x')

    def test_long_dossier_is_cut_with_a_section_index_from_the_file(self):
        self.s.deep_dive({'question': 'q', 'cwd': self.project})
        report = self.s._provenance_for('d1')['report_path']
        filler = 'x' * 200
        body = '# Answer\n' + '\n'.join(filler for _ in range(400)) + '\n## Risks\nlast fact\n'
        Path(report).write_text(body)
        self.assertGreater(len(body), self.s.DEEP_REPORT_MAX_CHARS)
        out = self.s._with_deep_report('d1', 'final msg')
        self.assertLess(len(out), self.s.DEEP_REPORT_MAX_CHARS + 2000)
        self.assertIn('line 1: # Answer', out)
        self.assertIn('line 402: ## Risks', out)  # past the cut, indexed from the file
        self.assertNotIn('last fact', out)
        self.assertTrue(out.endswith('=== FINAL MESSAGE ===\nfinal msg'))

    def _long_dossier(self, body):
        self.s.deep_dive({'question': 'q', 'cwd': self.project})
        Path(self.s._provenance_for('d1')['report_path']).write_text(body)

    def test_heading_heavy_dossier_still_fits_the_budget(self):
        self._long_dossier('\n'.join(f'# heading number {i} ' + 'y' * 30 for i in range(1500)))
        out = self.s._with_deep_report('d1', 'final msg')
        self.assertLessEqual(len(out), self.s.DEEP_RESULT_MAX_CHARS)
        self.assertIn('more headings', out)

    def test_fenced_comments_are_not_sections(self):
        code = '```python\n' + '\n'.join(f'# comment {i}' for i in range(400)) + '\n```\n'
        self._long_dossier('# Answer\n' + 'z' * 40_000 + '\n' + code + '## Risks\nr\n')
        out = self.s._with_deep_report('d1', 'f')
        self.assertNotIn('# comment', out.split('Sections')[-1])
        self.assertIn('## Risks', out)

    def test_cut_dossier_without_headings_has_no_empty_section_list(self):
        self._long_dossier('x' * 50_000)
        out = self.s._with_deep_report('d1', 'f')
        self.assertNotIn('Sections', out)
        self.assertIn('cut at line', out)

    def test_long_final_message_is_cut_to_the_budget(self):
        self._long_dossier('# A\n' + 'x' * 50_000)
        out = self.s._with_deep_report('d1', 'v' * 100_000 + 'VERDICT')
        self.assertLessEqual(len(out), self.s.DEEP_RESULT_MAX_CHARS)
        self.assertTrue(out.endswith('VERDICT'))

    def test_missing_dossier_is_reported_not_hidden(self):
        self.s.deep_dive({'question': 'q', 'cwd': self.project})
        out = self.s._with_deep_report('d1', 'final')
        self.assertIn('was NOT written', out)

    def test_start_expert_is_resident_and_named_for_sendmessage(self):
        self.assertTrue(self.s.start_expert({'cwd': self.project})['isError'])
        out = self.s.start_expert({'scope': 'external/sensors', 'cwd': self.project, 'focus': 'robot_cameras.py'})
        self.assertFalse(out['isError'])
        call = self.spawned[0]
        self.assertEqual(call['agent'], 'local-deep')
        self.assertIn('[EXPERT] external/sensors', call['name'])
        self.assertIn('SendMessage', out['content'][0]['text'])
        self.assertIn('robot_cameras.py', call['task'])
        self.assertIn('from=', call['task'])
        self.assertEqual(self.s._run_turn_cap('d1', 40), 0)  # resident: no turn ceiling
        self.assertEqual(self.s._provenance_for('d1')['deep_kind'], 'expert')

    def test_project_findings_lists_newest_first_filters_and_reads(self):
        folder = deep_tasks.findings_dir(self.s.FINDINGS_DIR, self.project)
        os.makedirs(folder)
        Path(folder, '20261001-000000-dive-old.md').write_text('# Old leak answer\n')
        Path(folder, '20261002-000000-review-new.md').write_text('# Review: PASS\n')
        os.utime(Path(folder, '20261001-000000-dive-old.md'), (1, 1))
        rows = json.loads(self.s.project_findings({'cwd': self.project})['content'][0]['text'])['findings']
        self.assertEqual([r['kind'] for r in rows], ['review', 'dive'])
        rows = json.loads(self.s.project_findings({'cwd': self.project, 'query': 'leak'})
                          ['content'][0]['text'])['findings']
        self.assertEqual([r['title'] for r in rows], ['Old leak answer'])
        text = self.s.project_findings({'cwd': self.project, 'read': '../../20261002-000000-review-new.md'})
        self.assertEqual(text['content'][0]['text'], '# Review: PASS\n')


class EngineStatusTests(unittest.TestCase):
    STATUS = {'service': 'strata', 'engine': '0.1.41', 'model': 'm', 'loaded': True,
              'cache_max_tokens': 262144, 'concurrency': {'serving': 1},
              'last_timings': {'predicted_per_second': 141.89}}
    METRICS = {'live': {'state': 'decoding', 'queued': 2},
               'conversation_cache': {'enabled': True, 'slots': 10, 'parked': 3,
                                      'evictions': 1, 'reused_tokens': 500}}

    def setUp(self):
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                test.auth = self.headers.get('Authorization')
                body = {'/v1/status': test.STATUS, '/metrics': test.METRICS}.get(self.path)
                self.send_response(200 if body else 404)
                self.end_headers()
                self.wfile.write(json.dumps(body or {}).encode())

            def log_message(self, *a):
                pass
        self.httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = os.path.join(self.tmp.name, 's.json')
        Path(self.settings).write_text(json.dumps({'env': {
            'ANTHROPIC_API_KEY': 'k',
            'CLAUDE_LOCAL_DELEGATE_ENGINE_URL': f'http://127.0.0.1:{self.httpd.server_port}/'}}))

    def test_snapshot_reads_status_and_metrics(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(engine_status.ENGINE_URL_KEY, None)
            snap = engine_status.snapshot(self.settings)
        self.assertEqual(self.auth, 'Bearer k')
        self.assertTrue(snap['known'])
        self.assertEqual((snap['serving'], snap['queued'], snap['decode_tok_s']), (1, 2, 141.9))
        self.assertEqual(snap['conversation_cache']['parked'], 3)
        self.assertEqual(snap['recommended_live_agents'], 2)

    def test_advice_by_load(self):
        os.environ.pop(engine_status.ENGINE_URL_KEY, None)
        snap = engine_status.snapshot(self.settings)
        self.assertIn('Room for 2 more', engine_status.advice(snap, 0))
        self.assertIn('more will only queue', engine_status.advice(snap, 2))
        self.assertIn('exceed the 10 cached', engine_status.advice(snap, 10))
        self.assertEqual(engine_status.advice({'known': False}, 0), '')
        self.assertIn('loading', engine_status.advice({'known': True, 'loaded': False}, 0))

    def test_unset_or_unreachable_engine_is_unknown_not_an_error(self):
        Path(self.settings).write_text(json.dumps({'env': {}}))
        os.environ.pop(engine_status.ENGINE_URL_KEY, None)
        self.assertFalse(engine_status.snapshot(self.settings)['known'])
        with patch.dict(os.environ, {engine_status.ENGINE_URL_KEY: 'http://127.0.0.1:9'}):
            snap = engine_status.snapshot(self.settings, timeout=0.5)
        self.assertFalse(snap['known'])
        self.assertIn('unreachable', snap['reason'])


if __name__ == '__main__':
    unittest.main()


class StatusMarkerTests(unittest.TestCase):
    """Claude Code marks a background session done only when the last 800 chars
    of its final message hold a `result:` line (outside a code fence)."""
    NATIVE_RESULT = __import__('re').compile(r'(?:^|\n)\s*result:\s*(.+?)\s*(?:\n|$)', __import__('re').I)

    def test_contract_and_personas_require_the_marker(self):
        s = load_server(tempfile.mkdtemp())
        self.assertIn('`result: ', s.REPORT_CONTRACT)
        self.assertIn('`failed: ', s.REPORT_CONTRACT)
        root = Path(__file__).resolve().parent.parent / 'agents'
        for persona in ('local-worker.md', 'local-deep.md', 'local-checker.md'):
            self.assertIn('result:', (root / persona).read_text(), persona)

    def test_checker_result_line_does_not_confuse_verdict_parsing(self):
        s = load_server(tempfile.mkdtemp())
        final = 'reasons...\nVERDICT: FAIL\nthe test still fails\nresult: checked, verdict FAIL'
        self.assertEqual(s._parse_verdict(final), 'FAIL')
        self.assertTrue(self.NATIVE_RESULT.search(final[-800:]))
