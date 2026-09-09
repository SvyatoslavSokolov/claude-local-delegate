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


# sync() flags a task stale at 900s. A takeover is a heavier act, so it needs a longer
# idle period on top of the no-live-children check.
TAKEOVER_IDLE_SECONDS = 1800


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
            takeover = False
            if t['owner'] != owner:
                # Owner-only release deadlocks when the owning session dies: it can never call
                # project_sync, so a handoff request is never delivered, and nothing expires.
                # Allow a release only when the reservation is long stale AND has no live
                # children -- the exact condition the module docstring gives as the reason
                # reservations do not expire. Anything else still refuses.
                idle = time.time() - t['updated_at']
                if not args.get('takeover'):
                    raise ValueError(
                        'task belongs to another session; use project_note to request a handoff, '
                        f'or pass takeover=true to release it (idle {int(idle)}s, needs '
                        f'{TAKEOVER_IDLE_SECONDS}s and no running delegates)')
                if idle < TAKEOVER_IDLE_SECONDS:
                    raise ValueError(
                        f'takeover refused: reservation was updated {int(idle)}s ago, '
                        f'under the {TAKEOVER_IDLE_SECONDS}s threshold')
                if args.get('status', t['status']) not in ('done', 'cancelled'):
                    raise ValueError('takeover may only release a task (done or cancelled)')
                if not runs_settled(t['runs']):
                    raise ValueError(
                        'takeover refused: the owning session still has delegated work running')
                takeover = True
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
            note = args.get('note', t['note'])
            if takeover:
                note = f'[taken over by {owner}] ' + (note or '')
            t['note'] = note[:2000]
            self._save(con, t)
            self._event(con, t, status, t['note'])
            return {'task': t}

    def sync(self, owner, args):
        project = str(Path(args['project']).expanduser().resolve())
        cursor = max(0, int(args.get('after_event', 0)))
        completed_limit = max(0, min(20, int(args.get('completed_limit', 3))))
        event_limit = max(0, min(50, int(args.get('event_limit', 20))))
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
            active = [t for t in relevant if t['status'] not in ('done', 'cancelled')]
            completed = [t for t in relevant if t['status'] in ('done', 'cancelled')]
            recent = completed[-completed_limit:] if completed_limit else []
            # Completed history can contain thousands of paths/runs and long notes.
            # A supervisor normally needs only a small handoff digest; the SQLite
            # board remains the source of truth for offline analysis.
            recent = [dict({k: t.get(k) for k in
                            ('id', 'task_key', 'summary', 'status', 'updated_at')},
                           note=(t.get('note') or '')[:240]) for t in recent]
            query_limit = event_limit + 1
            rows = con.execute('SELECT id, body FROM events WHERE project=? AND id>? ORDER BY id LIMIT ?',
                               (project, cursor, query_limit)).fetchall() if event_limit else []
            has_more = len(rows) > event_limit
            rows = rows[:event_limit]
            events = []
            for r in rows:
                event = {'event_id': r['id'], **json.loads(r['body'])}
                event['text'] = (event.get('text') or '')[:240]
                events.append(event)
            counts = {status: sum(t['status'] == status for t in relevant)
                      for status in ('active', 'paused', 'waiting', 'done', 'cancelled')}
            return {'session_id': owner, 'tasks': active + recent,
                    'task_counts': counts, 'completed_returned': len(recent),
                    'events': events, 'has_more_events': has_more,
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
            for t in tasks:
                if t['status'] not in ('active', 'paused') or not (writes or t['mode'] == 'write'):
                    continue
                hit = next((p for p in t['paths'] if overlap(cwd, p)), None)
                if hit is None:
                    continue
                # Name the blocker. "project has reserved work" alone sent a user hunting
                # through the board by hand for the one task that was in their way.
                mine = t['owner'] == owner
                raise ValueError(
                    f"blocked by reservation {t['task_key']!r} (id {t['id']}, "
                    f"{'yours' if mine else 'owned by ' + t['owner']}, mode {t['mode']}, "
                    f"status {t['status']}) on path {hit!r}. "
                    + ('Pass its task_id to delegate under it, or claim a task with '
                       'non-overlapping paths.' if mine else
                       'Ask that session to release it, or use project_note to request a handoff.'))
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
    schema('project_sync', 'Compact shared task board: all unfinished tasks, a small recent-completed digest, aggregate counts, and bounded incremental events. Call before work and at checkpoints. Increase limits only when history is actually needed.',
           {'project': STRING, 'after_event': {'type': 'integer', 'minimum': 0},
            'completed_limit': {'type': 'integer', 'minimum': 0, 'maximum': 20, 'description': 'Recent terminal task digests to return; default 3.'},
            'event_limit': {'type': 'integer', 'minimum': 0, 'maximum': 50, 'description': 'Incremental events to return; default 20.'}}, ['project']),
    schema('task_claim', 'Atomically reserve a unique task and literal file/directory paths. Read/read may overlap; writes exclude reads and writes. Conflicts/dependencies create waiting tasks. Only active tasks may start.',
           {'project': STRING, 'task_key': STRING, 'summary': STRING, 'paths': {'type': 'array', 'items': STRING},
            'mode': {'type': 'string', 'enum': ['read', 'write']}, 'depends_on': {'type': 'array', 'items': STRING}}, ['project', 'task_key']),
    schema('task_update', 'Update your task. active retries waiting dependencies/locks; paused retains locks; waiting releases locks; done/cancelled release only after children settle. Record evidence in note. Nothing expires on its own, but a reservation whose owning session has gone away can be released with takeover=true: only to done or cancelled, only once it has been idle past the threshold, and only when it has no delegated work still running. The release is stamped into its note.',
           {'task_id': STRING, 'status': {'type': 'string', 'enum': ['active', 'paused', 'waiting', 'done', 'cancelled']}, 'note': STRING,
            'takeover': {'type': 'boolean', 'description': "Release another session's abandoned reservation. Refused unless it is idle past the threshold and has no running delegates."}}, ['task_id']),
    schema('project_note', 'Post a short coordination note or handoff/pause request for the other supervisor. Delivered when they call project_sync, not a push notification or forced pause.',
           {'project': STRING, 'task_id': STRING, 'message': STRING}, ['project', 'message']),
]
