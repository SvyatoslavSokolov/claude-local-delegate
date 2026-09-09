"""Unit tests for the coordination.Board reservation API (no model calls, no network)."""
import os
import unittest
from pathlib import Path
import tempfile

from coordination import Board


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = self.tmp.name
        self.board = Board(str(Path(self.project) / 'board.sqlite'))
        self.settled = lambda runs: True
        self.unsettled = lambda runs: len(runs) == 0

    def claim(self, key, mode='write', paths=None, owner='alice', depends_on=None):
        args = {'project': self.project, 'task_key': key, 'mode': mode}
        if paths is not None:
            args['paths'] = paths
        if depends_on is not None:
            args['depends_on'] = depends_on
        return self.board.claim(owner, args)

    def test_write_write_conflict_including_directory_child(self):
        a = self.claim('a', paths=['a.txt'])
        self.assertEqual(a['task']['status'], 'active')
        # Direct write/write conflict on the same file.
        b = self.claim('b', paths=['a.txt'])
        self.assertEqual(b['task']['status'], 'waiting')
        self.assertTrue(any(blk['task_id'] == a['task']['id'] for blk in b['task']['blockers']))
        # Directory-child: write on the parent directory vs a child file inside it.
        os.mkdir(os.path.join(self.project, 'src'))
        d = self.claim('d', paths=['src'])
        self.assertEqual(d['task']['status'], 'active')
        e = self.claim('e', paths=['src/child.txt'])
        self.assertEqual(e['task']['status'], 'waiting')
        self.assertTrue(any(blk['task_id'] == d['task']['id'] for blk in e['task']['blockers']))

    def test_read_read_succeeds(self):
        a = self.claim('r1', mode='read', paths=['a.txt'])
        b = self.claim('r2', mode='read', paths=['a.txt'])
        self.assertEqual(a['task']['status'], 'active')
        self.assertEqual(b['task']['status'], 'active')

    def test_duplicate_key_returns_existing(self):
        a = self.claim('dup')
        self.assertTrue(a['created'])
        b = self.claim('dup')
        self.assertFalse(b['created'])
        self.assertEqual(b['task']['id'], a['task']['id'])

    def test_dependency_waits_then_active_after_done(self):
        a = self.claim('dep', paths=['x.txt'])
        self.assertEqual(a['task']['status'], 'active')
        b = self.claim('consumer', paths=['y.txt'], depends_on=[a['task']['id']])
        self.assertEqual(b['task']['status'], 'waiting')
        # Prerequisite still not done -> consumer remains blocked.
        self.board.update('alice', {'task_id': b['task']['id'], 'status': 'active'}, self.settled)
        self.assertEqual(b['task']['status'], 'waiting')
        # Mark prerequisite done.
        self.board.update('alice', {'task_id': a['task']['id'], 'status': 'done'}, self.settled)
        # Now the consumer can go active.
        result = self.board.update('alice', {'task_id': b['task']['id'], 'status': 'active'}, self.settled)
        self.assertEqual(result['task']['status'], 'active')

    def test_wrong_owner_cannot_update(self):
        a = self.claim('own', owner='alice')
        with self.assertRaises(ValueError):
            self.board.update('bob', {'task_id': a['task']['id'], 'status': 'paused'}, self.settled)

    def test_paused_retains_reservation(self):
        a = self.claim('p1', paths=['a.txt'])
        self.assertEqual(a['task']['status'], 'active')
        self.board.update('alice', {'task_id': a['task']['id'], 'status': 'paused'}, self.settled)
        # A paused task still holds its paths, so a conflicting write is blocked.
        b = self.claim('p2', paths=['a.txt'])
        self.assertEqual(b['task']['status'], 'waiting')
        self.assertTrue(any(blk['task_id'] == a['task']['id'] for blk in b['task']['blockers']))

    def test_child_not_settled_prevents_done(self):
        a = self.claim('c1', paths=['a.txt'])
        self.board.attach(a['task']['id'], 'run-123')
        with self.assertRaises(ValueError):
            self.board.update('alice', {'task_id': a['task']['id'], 'status': 'done'}, self.unsettled)

    def test_symlink_outside_project_rejected(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        os.symlink(outside.name, os.path.join(self.project, 'link'))
        with self.assertRaises(ValueError):
            self.claim('s1', paths=['link'])

    def test_sync_compacts_completed_history_and_events(self):
        for i in range(6):
            task = self.claim('done-%d' % i, paths=['done-%d.txt' % i])['task']
            self.board.update('alice', {
                'task_id': task['id'], 'status': 'done', 'note': 'x' * 500,
            }, self.settled)
        data = self.board.sync('reader', {
            'project': self.project, 'completed_limit': 2, 'event_limit': 3,
        })
        self.assertEqual(data['task_counts']['done'], 6)
        self.assertEqual(data['completed_returned'], 2)
        self.assertEqual(len(data['tasks']), 2)
        self.assertNotIn('paths', data['tasks'][0])
        self.assertEqual(len(data['tasks'][0]['note']), 240)
        self.assertEqual(len(data['events']), 3)
        self.assertTrue(data['has_more_events'])


if __name__ == '__main__':
    unittest.main()
