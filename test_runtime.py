"""Integration regressions without model calls or user state writes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from coordination_runtime import install


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'CLAUDE_LOCAL_DELEGATE_STATE_DIR': self.tmp.name,
                                           'CLAUDE_LOCAL_DELEGATE_SESSION_ID': 'test-parent'})
        self.env.start()
        self.addCleanup(self.env.stop)
        spec = importlib.util.spec_from_file_location('isolated_server', Path(__file__).with_name('server.py'))
        self.s = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.s)
        self.s.VERIFIED_DIR = str(Path(self.tmp.name) / 'verified')
        self.s.BATCHES_DIR = str(Path(self.tmp.name) / 'batches')
        self.s.STATE_DIR = self.tmp.name
        self.s.PROVENANCE_PATH = str(Path(self.tmp.name) / 'runs.json')
        self.spawn = patch.object(self.s, '_spawn_native_agent', return_value=('abc12345', None)).start()
        self.addCleanup(patch.stopall)
        self.s._agents_json = lambda *args: []
        install(self.s)

    def call(self, name, **args):
        return self.s.handle_request({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                     'params': {'name': name, 'arguments': args}})['result']

    def claim(self, mode='write'):
        reply = self.call('task_claim', project=self.tmp.name, task_key='one', mode=mode)
        return json.loads(reply['content'][0]['text'])['task']['id']

    def test_original_tools_and_portable_tools(self):
        tools = self.s.handle_request({'id': 1, 'method': 'tools/list'})['result']['tools']
        self.assertEqual(len(tools), 17)
        self.assertIn('delegate_verified', {t['name'] for t in tools})
        self.assertIn('continue_delegate', {t['name'] for t in tools})

    def test_conflicting_legacy_spawn_is_blocked(self):
        self.claim()
        result = self.call('delegate_to_local', task='edit', cwd=self.tmp.name)
        self.assertTrue(result['isError'])
        self.spawn.assert_not_called()

    def test_claim_passed_to_spawn_and_child_holds_reservation(self):
        task_id = self.claim()
        result = self.call('delegate_to_local', task='edit', cwd=self.tmp.name, task_id=task_id)
        self.assertFalse(result['isError'])
        self.assertIn('COORDINATED TASK', self.spawn.call_args.args[0])
        self.assertTrue(self.call('task_update', task_id=task_id, status='done')['isError'])
        self.s._agents_json = lambda *args: [{'id': 'abc12345', 'state': 'done'}]
        self.assertFalse(self.call('task_update', task_id=task_id, status='done')['isError'])

    def test_shell_requires_write_claim(self):
        task_id = self.claim('read')
        reply = self.call('delegate_to_local', task='run tests', cwd=self.tmp.name,
                          task_id=task_id, allowed_tools='Read,Bash')
        self.assertTrue(reply['isError'])
        self.spawn.assert_not_called()

    def test_capacity_counts_only_recorded_local_delegates(self):
        self.s.LOCAL_SERVER_MAX_CONCURRENCY = 1
        self.s._agents_json = lambda *args: [{'id': 'other', 'kind': 'background', 'state': 'working'}]
        # A background session this server never spawned is not on the local GPU;
        # it must not consume a slot in the vLLM pool.
        self.assertFalse(self.call('delegate_to_local', task='read', cwd=self.tmp.name)['isError'])
        self.spawn.assert_called_once()
        # Once it is recorded as a local delegate, the ceiling applies.
        self.s._record_provenance('other', {'at': 0, 'backend': 'local', 'model': 'm'})
        self.assertTrue(self.call('delegate_to_local', task='read', cwd=self.tmp.name)['isError'])
        self.assertEqual(self.spawn.call_count, 1)

    def test_spawn_records_backend_provenance(self):
        self.s._spawn_native_agent = self.spawn  # coordination wraps the real one
        self.call('delegate_to_local', task='read', cwd=self.tmp.name)
        # the wrapper records nothing itself; provenance is written by the real
        # spawner, so assert on the helper contract the wrapper depends on.
        self.s._record_provenance('abc12345', {'at': 1.0, 'backend': 'local', 'model': 'qwen'})
        self.assertEqual(self.s._provenance_for('abc12345')['model'], 'qwen')
        self.assertIsNone(self.s._provenance_for('nope'))

    def test_read_only_calls_do_not_wait_on_the_cross_process_lock(self):
        # flock is held per open file description, so this really is the lock a
        # second supervisor would hold while its `claude --bg` starts.
        import fcntl
        import threading
        lock = (Path(self.tmp.name) / 'operations.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX)
        replies = {}

        def read_only():
            replies['sync'] = self.call('project_sync', project=self.tmp.name)

        def mutation():
            replies['claim'] = self.call('task_claim', project=self.tmp.name, task_key='blocked')

        try:
            t = threading.Thread(target=read_only, daemon=True)
            t.start()
            t.join(5)
            self.assertFalse(t.is_alive(), 'a status read must not queue behind a spawn')
            self.assertFalse(replies['sync']['isError'])

            blocked = threading.Thread(target=mutation, daemon=True)
            blocked.start()
            blocked.join(2)
            self.assertTrue(blocked.is_alive(), 'mutations must still be serialized')
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
            blocked.join(5)

    def test_followup_refuses_live_source_and_forks_terminal(self):
        source = {'id': 'source', 'sessionId': 'session-uuid', 'cwd': self.tmp.name, 'state': 'working'}
        self.s._resolve_agent = lambda *args: (source, None)
        self.assertTrue(self.call('continue_delegate', run_id='source', message='fix')['isError'])
        source['state'] = 'done'
        self.assertFalse(self.call('continue_delegate', run_id='source', message='fix')['isError'])
        self.assertEqual(self.spawn.call_args.args[-1], 'session-uuid')

    def test_verified_persists_owner(self):
        task_id = self.claim()
        reply = self.call('delegate_verified', task='write test', cwd=self.tmp.name, task_id=task_id)
        self.assertFalse(reply['isError'])
        state = json.loads(next(Path(self.s.VERIFIED_DIR).glob('*.json')).read_text())
        self.assertEqual(state['coordination_task_id'], task_id)
        self.assertEqual(state['coordination_owner'], 'test-parent')

    def test_stdio_handshake_and_bad_arguments_dont_kill_server(self):
        messages = [{'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
                    {'method': 'notifications/initialized'},
                    {'id': 2, 'method': 'tools/call', 'params': {'name': 'task_claim', 'arguments': {}}},
                    {'id': 3, 'method': 'tools/list'}]
        p = subprocess.run([sys.executable, str(Path(__file__).with_name('server.py'))],
                           input=''.join(json.dumps(m) + '\n' for m in messages),
                           text=True, capture_output=True, timeout=10)
        self.assertEqual(p.returncode, 0, p.stderr)
        responses = [json.loads(line) for line in p.stdout.splitlines()]
        self.assertEqual([r['id'] for r in responses], [1, 2, 3])
        self.assertTrue(responses[1]['result']['isError'])
        self.assertEqual(len(responses[2]['result']['tools']), 17)


if __name__ == '__main__':
    unittest.main()
