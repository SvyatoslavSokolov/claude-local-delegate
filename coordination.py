"""Cross-client task reservations. SQLite transactions arbitrate independent MCPs.

Reservations are cooperative, not filesystem access controls. They never expire:
an absent supervisor may still have a live child writing its files.
"""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


def overlap(a, b):
    return a == b or a.startswith(b.rstrip('/') + '/') or b.startswith(a.rstrip('/') + '/')


class Board:
    def __init__(self, path):
        self.path = Path(path)

    @contextlib.contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, project TEXT NOT NULL, task_key TEXT NOT NULL, owner TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(project, task_key))')
            con.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL, body TEXT NOT NULL)')
            con.execute('BEGIN IMMEDIATE')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _all(self, con):
        return [json.loads(r['body']) for r in con.execute('SELECT body FROM tasks ORDER BY rowid')]

    def _save(self, con, t):
        t['updated_at'] = time.time()
        con.execute('INSERT INTO tasks VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,body=excluded.body',
                    (t['id'], t['project'], t['task_key'], t['owner'], json.dumps(t)))

    def _event(self, con, t, kind, text=''):
        body = {'task_id': t['id'], 'owner': t['owner'], 'kind': kind, 'text': text[:2000], 'at': time.time()}
        con.execute('INSERT INTO events(project,body) VALUES (?,?)', (t['project'], json.dumps(body)))

    def _blockers(self, task, tasks):
        blockers = []
        by_id = {t['id']: t for t in tasks}
        for dep in task['depends_on']:
            if dep not in by_id or by_id[dep]['status'] != 'done':
                blockers.append({'task_id': dep, 'reason': 'dependency not done'})
        for other in tasks:
            if other['id'] == task['id'] or other['status'] not in ('active', 'paused'):
                continue
            if task['mode'] == other['mode'] == 'read':
                continue
            if any(overlap(a, b) for a in task['paths'] for b in other['paths']):
                blockers.append({'task_id': other['id'], 'owner': other['owner'], 'reason': 'overlapping paths'})
        return blockers

    def claim(self, owner, args):
        project = str(Path(args['project']).expanduser().resolve())
        if not Path(project).is_dir():
            raise ValueError('project must be an existing directory')
        key = args['task_key'].strip()
        if not key or len(key) > 200:
            raise ValueError('task_key must contain 1..200 characters')
        mode = args.get('mode', 'write')
        if mode not in ('read', 'write'):
            raise ValueError('mode must be read or write')
        paths = args.get('paths') or ['.']
        if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
            raise ValueError('paths must be a list of nonempty paths (no globs)')
        resolved = []
        for p in paths:
            if any(c in p for c in '*?['):
                raise ValueError('paths are literal files/directories, not globs')
            path = str((Path(project) / p).resolve())
            if os.path.commonpath([project, path]) != project:
                raise ValueError('all paths must stay within project, including symlink targets')
            resolved.append(path)
        dependencies = args.get('depends_on', [])
        if not isinstance(dependencies, list) or not all(isinstance(d, str) for d in dependencies):
            raise ValueError('depends_on must be an array of task IDs')
        with self.transaction() as con:
            tasks = self._all(con)
            for t in tasks:
                if t['project'] == project and t['task_key'] == key:
                    return {'created': False, 'task': t, 'note': 'Existing task: do not duplicate it. Use task_update to retry your waiting task.'}
            if any(d not in {t['id'] for t in tasks} for d in dependencies):
                raise ValueError('unknown dependency')
            t = {'id': uuid.uuid4().hex, 'project': project, 'task_key': key, 'owner': owner,
                 'summary': args.get('summary', key)[:2000], 'mode': mode, 'paths': sorted(set(resolved)),
                 'depends_on': dependencies, 'status': 'waiting', 'runs': [], 'note': '', 'created_at': time.time()}
            t['blockers'] = self._blockers(t, tasks)
            if not t['blockers']:
                t['status'] = 'active'
            self._save(con, t)
            self._event(con, t, 'claim')
            return {'created': True, 'task': t}

    def update(self, owner, args, runs_settled):
        with self.transaction() as con:
            tasks = self._all(con)
            t = next((t for t in tasks if t['id'] == args['task_id']), None)
            if not t:
                raise ValueError('unknown task_id')
            if t['owner'] != owner:
                raise ValueError('task belongs to another session; use project_note to request a handoff')
            status = args.get('status', t['status'])
            if status not in ('active', 'paused', 'waiting', 'done', 'cancelled'):
                raise ValueError('invalid status')
            if t['status'] in ('done', 'cancelled'):
                raise ValueError('task is terminal; use a new task_key for additional work')
            if status == 'paused' and t['status'] == 'waiting':
                raise ValueError('waiting task must acquire its reservation before it can be paused')
            if status in ('done', 'cancelled', 'waiting') and not runs_settled(t['runs']):
                raise ValueError('cannot release reservation: delegated work is still running or its state is unknown')
            if status == 'active':
                t['blockers'] = self._blockers(t, tasks)
                if t['blockers']:
                    return {'task': t, 'blocked': True}
            t['status'] = status
            t['note'] = args.get('note', t['note'])[:2000]
            self._save(con, t)
            self._event(con, t, status, t['note'])
            return {'task': t}

    def sync(self, owner, args):
        project = str(Path(args['project']).expanduser().resolve())
        cursor = max(0, int(args.get('after_event', 0)))
        with self.transaction() as con:
            tasks = self._all(con)
            relevant = [t for t in tasks if overlap(project, t['project'])]
            for t in relevant:
                t['stale'] = time.time() - t['updated_at'] > 900
                if t['status'] == 'waiting':
                    t['blockers'] = self._blockers(t, tasks)
                    # Nothing starts by itself, but the supervisor should not have
                    # to re-derive which of its parked tasks are now startable.
                    t['ready'] = not t['blockers']
            rows = con.execute('SELECT id, body FROM events WHERE project=? AND id>? ORDER BY id LIMIT 50', (project, cursor)).fetchall()
            return {'session_id': owner, 'tasks': [t for t in relevant if t['status'] not in ('done', 'cancelled')] + [t for t in relevant if t['status'] in ('done', 'cancelled')][-20:],
                    'events': [{'event_id': r['id'], **json.loads(r['body'])} for r in rows],
                    'next_event': rows[-1]['id'] if rows else cursor,
                    'note': 'Reservations are cooperative. Stale does not mean safe to release. Paused tasks retain their paths.'}

    def note(self, owner, args):
        project = str(Path(args['project']).expanduser().resolve())
        with self.transaction() as con:
            self._event(con, {'id': args.get('task_id', ''), 'owner': owner, 'project': project}, 'note', args['message'])
        return {'posted': True}

    def authorize(self, owner, task_id, cwd, writes):
        """Called before spawn; enclosing server lock prevents release/spawn races."""
        cwd = str(Path(cwd).resolve())
        with self.transaction() as con:
            tasks = self._all(con)
            if task_id:
                t = next((t for t in tasks if t['id'] == task_id), None)
                if not t or t['owner'] != owner or t['status'] != 'active':
                    raise ValueError('task_id must identify an active reservation owned by this session')
                if os.path.commonpath([cwd, t['project']]) != t['project']:
                    raise ValueError('delegate cwd must be within reserved project')
                if writes and t['mode'] != 'write':
                    raise ValueError('shell/write delegation needs a write reservation')
                return t
            if any(t['status'] in ('active', 'paused') and any(overlap(cwd, p) for p in t['paths'])
                   and (writes or t['mode'] == 'write') for t in tasks):
                raise ValueError('project has reserved work; claim a task and pass task_id before delegating')
            return None

    def owner_for_run(self, run_id):
        with self.transaction() as con:
            return next((t['owner'] for t in self._all(con) if run_id in t['runs']), None)

    def attach(self, task_id, run_id):
        if not task_id:
            return
        with self.transaction() as con:
            t = next(t for t in self._all(con) if t['id'] == task_id)
            t['runs'].append(run_id)
            self._save(con, t)


def schema(name, description, properties, required):
    return {'name': name, 'description': description, 'inputSchema': {'type': 'object', 'properties': properties, 'required': required}}


STRING = {'type': 'string'}
TOOLS = [
    schema('project_sync', 'Cheap shared Claude/Codex task board and incremental notes. Call before starting work and at checkpoints. No model invocation.',
           {'project': STRING, 'after_event': {'type': 'integer', 'minimum': 0}}, ['project']),
    schema('task_claim', 'Atomically reserve a unique task and literal file/directory paths. Read/read may overlap; writes exclude reads and writes. Conflicts/dependencies create waiting tasks. Only active tasks may start.',
           {'project': STRING, 'task_key': STRING, 'summary': STRING, 'paths': {'type': 'array', 'items': STRING},
            'mode': {'type': 'string', 'enum': ['read', 'write']}, 'depends_on': {'type': 'array', 'items': STRING}}, ['project', 'task_key']),
    schema('task_update', 'Update your task. active retries waiting dependencies/locks; paused retains locks; waiting releases locks; done/cancelled release only after children settle. Record evidence in note. No automatic stale lock expiry.',
           {'task_id': STRING, 'status': {'type': 'string', 'enum': ['active', 'paused', 'waiting', 'done', 'cancelled']}, 'note': STRING}, ['task_id']),
    schema('project_note', 'Post a short coordination note or handoff/pause request for the other supervisor. Delivered when they call project_sync, not a push notification or forced pause.',
           {'project': STRING, 'task_id': STRING, 'message': STRING}, ['project', 'message']),
]
