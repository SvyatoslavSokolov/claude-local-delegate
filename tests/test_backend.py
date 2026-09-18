import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import local_backend
import server


class BackendTests(unittest.TestCase):
    def test_settings_first_model_and_environment_are_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'local.json'
            settings.write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': 'http://localhost:4000',
                                                    'ANTHROPIC_MODEL': 'local-test', 'ANTHROPIC_API_KEY': 'test-only'}}))
            with patch.object(server, '_default_settings_path', return_value=str(settings)), \
                 patch.object(server, 'STATE_DIR', tmp), \
                 patch.object(server, 'PROVENANCE_PATH', str(Path(tmp) / 'provenance.json')), \
                 patch.dict(os.environ, {'CLAUDE_CODE_OAUTH_TOKEN': 'cloud', 'ANTHROPIC_AUTH_TOKEN': 'cloud'}), \
                 patch.object(server.subprocess, 'run') as run:
                run.return_value.stdout = b'backgrounded abc12345'
                run.return_value.returncode = 0
                rid, error = server._spawn_native_agent('hello', 'Read', tmp, 'probe')
            self.assertIsNone(error)
            self.assertEqual(rid, 'abc12345')
            command = run.call_args.args[0]
            self.assertEqual(command[:4], [server.CLAUDE_BIN, '--settings', str(settings), '--bg'])
            self.assertEqual(command[command.index('--model') + 1], 'local-test')
            env = run.call_args.kwargs['env']
            self.assertEqual(env['ANTHROPIC_BASE_URL'], 'http://localhost:4000')
            self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
            self.assertNotIn('ANTHROPIC_AUTH_TOKEN', env)

    def test_missing_route_never_falls_back_to_subscription(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'settings.json'
            p.write_text('{"env":{}}')
            with patch.object(server, '_default_settings_path', return_value=str(p)), \
                 patch.object(server.subprocess, 'run') as run:
                rid, error = server._spawn_native_agent('hello', 'Read', tmp, 'probe')
            self.assertIsNone(rid)
            self.assertIn('ANTHROPIC_BASE_URL', error)
            run.assert_not_called()


class DoctorCheckTests(unittest.TestCase):
    def _write(self, tmp, env):
        p = Path(tmp) / 'settings.json'
        p.write_text(json.dumps({'env': env}))
        return p

    def test_success_returns_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, {'ANTHROPIC_BASE_URL': 'http://localhost:4000',
                                  'ANTHROPIC_MODEL': 'local-test',
                                  'ANTHROPIC_API_KEY': 'test-only'})
            with patch.object(local_backend.urllib.request, 'urlopen') as urlopen:
                urlopen.return_value.__enter__ = lambda self: self
                urlopen.return_value.__exit__ = lambda self, *a: None
                result = local_backend.doctor_check(str(p), timeout=5.0)
            self.assertTrue(result['ok'])
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['base_url'], 'http://localhost:4000')
            self.assertEqual(result['model'], 'local-test')
            self.assertIsNone(result['error'])
            urlopen.assert_called_once()
            self.assertEqual(urlopen.call_args[1]['timeout'], 5.0)

    def test_missing_key_reports_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, {'ANTHROPIC_MODEL': 'local-test'})
            with patch.object(local_backend.urllib.request, 'urlopen') as urlopen:
                result = local_backend.doctor_check(str(p))
            self.assertFalse(result['ok'])
            self.assertEqual(result['status'], 'config-error')
            self.assertIn('ANTHROPIC_BASE_URL', result['error'])
            self.assertIsNone(result['base_url'])
            urlopen.assert_not_called()

    def test_unreachable_reports_error(self):
        import urllib.error
        with tempfile.TemporaryDirectory() as tmp:
            p = self._write(tmp, {'ANTHROPIC_BASE_URL': 'http://localhost:4000',
                                  'ANTHROPIC_MODEL': 'local-test',
                                  'ANTHROPIC_API_KEY': 'test-only'})
            with patch.object(local_backend.urllib.request, 'urlopen',
                              side_effect=urllib.error.URLError('no route to host')) as urlopen:
                result = local_backend.doctor_check(str(p), timeout=1.0)
            self.assertFalse(result['ok'])
            self.assertEqual(result['status'], 'unreachable')
            self.assertEqual(result['base_url'], 'http://localhost:4000')
            self.assertEqual(result['model'], 'local-test')
            self.assertIn('no route to host', result['error'])
            urlopen.assert_called_once()

    def test_default_path_uses_expanduser(self):
        default = os.path.expanduser('~/.claude/vllm.delegate.settings.json')
        with patch.object(local_backend, 'profile') as prof, \
             patch.object(local_backend.urllib.request, 'urlopen') as urlopen:
            prof.return_value = {'ANTHROPIC_BASE_URL': 'http://localhost:4000',
                                 'ANTHROPIC_MODEL': 'local-test',
                                 'ANTHROPIC_API_KEY': 'test-only'}
            urlopen.return_value.__enter__ = lambda self: self
            urlopen.return_value.__exit__ = lambda self, *a: None
            result = local_backend.doctor_check()
        self.assertTrue(result['ok'])
        prof.assert_called_once_with(default)


if __name__ == '__main__':
    unittest.main()
