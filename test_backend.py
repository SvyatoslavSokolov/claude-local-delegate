import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
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


if __name__ == '__main__':
    unittest.main()
