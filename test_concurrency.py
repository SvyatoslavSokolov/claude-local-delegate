import json
import multiprocessing
from pathlib import Path
import tempfile
import unittest
from coordination import Board


def claim_at_once(db, project, key, start, out):
    start.wait(5)
    try:
        task = Board(db).claim(key, {'project': project, 'task_key': key})['task']
        out.put(task['status'])
    except Exception as e:
        out.put(repr(e))


class ProcessTests(unittest.TestCase):
    def test_independent_servers_cannot_claim_same_write_scope(self):
        with tempfile.TemporaryDirectory() as project:
            ctx = multiprocessing.get_context('spawn')
            gate, out = ctx.Event(), ctx.Queue()
            processes = [ctx.Process(target=claim_at_once,
                                     args=(str(Path(project) / 'board.sqlite'), project, str(i), gate, out))
                         for i in range(6)]
            try:
                for p in processes:
                    p.start()
                gate.set()
                statuses = [out.get(timeout=10) for _ in processes]
                self.assertEqual(statuses.count('active'), 1, statuses)
                self.assertEqual(statuses.count('waiting'), 5, statuses)
                for p in processes:
                    p.join(10)
                    self.assertEqual(p.exitcode, 0)
            finally:
                for p in processes:
                    if p.is_alive():
                        p.terminate()
                        p.join()
                out.close()


if __name__ == '__main__':
    unittest.main()
