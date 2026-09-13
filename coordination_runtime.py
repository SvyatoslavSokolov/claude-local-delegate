"""Client-neutral supervision and cross-process coordination for the MCP."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time
import uuid
from coordination import Board, TOOLS, STRING, schema

# Tools that only read state. They bypass the cross-process operations lock:
# a status poll must stay cheap even while another supervisor is spawning.
READ_ONLY_CALLS = frozenset({
    'project_sync', 'local_backend_info', 'check_delegate_status', 'watch_delegate',
    'get_delegate_result', 'check_fanout_status', 'get_fanout_result', 'rate_delegate',
})
# Tools whose slow step is a `claude --bg` subprocess and which hold no
# half-written state across it, so the lock may be released while it runs.
# The verified loop is deliberately NOT here: its state machine advances across
# the spawn, and two supervisors polling one vid must not both launch a checker.
SPAWN_CALLS = frozenset({'delegate_to_local', 'fan_out_to_local', 'continue_delegate'})


def install(server):
    if getattr(server, '_coordination_installed', False):
        return
    server._coordination_installed = True
    root = Path(os.environ.get('CLAUDE_LOCAL_DELEGATE_STATE_DIR', '~/.claude-local-delegate')).expanduser()
    board = Board(root / 'coordination.sqlite3')
    owner = os.environ.get('CLAUDE_LOCAL_DELEGATE_SESSION_ID') or 'session-' + uuid.uuid4().hex
    current_task = None
    depth = 0
    held = None            # the flock'ed file object while depth > 0
    release_on_spawn = False   # set per request: may this call drop the lock to spawn?

    @contextmanager
    def locked():
        nonlocal depth, held
        if depth:
            depth += 1
            try:
                yield
            finally:
                depth -= 1
            return
        root.mkdir(parents=True, exist_ok=True)
        with (root / 'operations.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            depth, held = 1, lock
            try:
                yield
            finally:
                depth, held = 0, None
                fcntl.flock(lock, fcntl.LOCK_UN)

    @contextmanager
    def unlocked():
        """Drop the cross-process lock for a slow, self-contained step and take it
        back after. `claude --bg` can take up to 120s to return; holding the global
        operations lock across it froze every other supervisor's reads AND spawns.
        Admission is already decided (and recorded as an in-flight ticket) before
        the lock is dropped, so the pool ceiling still holds while it is open."""
        nonlocal depth
        if not depth or held is None:
            yield
            return
        was, depth = depth, 0
        fcntl.flock(held, fcntl.LOCK_UN)
        try:
            yield
        finally:
            fcntl.flock(held, fcntl.LOCK_EX)
            depth = was

    tickets_path = root / 'inflight.json'
    TICKET_TTL = 150   # a shade over the 120s spawn timeout in server._spawn_native_agent

    def _tickets(prune=True):
        try:
            data = json.loads(tickets_path.read_text())
        except (OSError, ValueError):
            return {}
        live = data.get('tickets') if isinstance(data.get('tickets'), dict) else {}
        if prune:
            now = time.time()
            live = {k: v for k, v in live.items() if now - v.get('at', 0) < TICKET_TTL}
        return live

    def _write_tickets(tickets):
        root.mkdir(parents=True, exist_ok=True)
        tmp = tickets_path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'tickets': tickets}))
        tmp.replace(tickets_path)

    def take_ticket():
        """Reserve a pool slot for a spawn that has not returned an id yet."""
        tid = uuid.uuid4().hex[:8]
        tickets = _tickets()
        tickets[tid] = {'owner': owner, 'at': time.time()}
        _write_tickets(tickets)
        return tid

    def drop_ticket(tid):
        tickets = _tickets()
        tickets.pop(tid, None)
        _write_tickets(tickets)

    def result(data):
        return {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}], 'isError': False}

    def settled(runs):
        if not runs:
            return True
        for path in Path(server.VERIFIED_DIR).glob('*.json'):
            state = json.loads(path.read_text())
            if state.get('phase') not in ('passed', 'failed') and (state.get('current_worker_id') in runs or state.get('current_checker_id') in runs):
                return False
        agents = server._agents_json()
        if agents is None:
            return False
        lookup = {a.get('id'): a for a in agents}
        return all(r in lookup and (lookup[r].get('state') or lookup[r].get('status'))
                   in ('completed', 'done', 'idle', 'failed', 'stopped') for r in runs)

    original_spawn = server._spawn_native_agent

    def spawn(task, allowed_tools, cwd, name, permission_mode=None,
              disallowed_tools=None, agent=None, announce_plan=None, **spawn_kwargs):
        with locked():
            # A writer is anything that is NOT the server's authoritative
            # read-only set (Bash can write; WebSearch/WebFetch/LSP/NotebookRead
            # cannot). Allowlists are not filesystem isolation, so a writer
            # needs a write reservation.
            writes = not server._is_read_only(allowed_tools)
            try:
                reservation = board.authorize(owner, current_task, cwd, writes)
            except ValueError as exc:
                return None, str(exc)
            roster = server._agents_json()
            if roster is None:
                return None, 'Cannot inspect native roster; refusing an unaccounted spawn. Retry when Claude supervisor is available.'
            # The pool being protected is the local vLLM's, so only runs THIS server
            # recorded as local delegates count against it (shared file, so a Codex
            # supervisor's delegates count too). A paid Anthropic background session
            # -- the supervisor itself, for one -- sits in the same native roster but
            # never touches the GPU, and used to consume a slot it did not use.
            recorded = server._load_provenance()
            settled_states = ('done', 'completed', 'idle', 'failed', 'stopped')
            active = [a for a in roster if a.get('kind') == 'background'
                      and (a.get('state') or a.get('status')) not in settled_states
                      and recorded.get(a.get('id'), {}).get('backend') == 'local']
            pending = _tickets()
            if len(active) + len(pending) >= server.LOCAL_SERVER_MAX_CONCURRENCY:
                return None, ('Local pool full (' + str(len(active)) + ' local delegate(s) running, '
                              + str(len(pending)) + ' spawning). Retry after one settles.')
            if reservation and writes:
                lookup = {a.get('id'): a for a in roster}
                terminal = ('done', 'completed', 'idle', 'failed', 'stopped')
                if any(r not in lookup or (lookup[r].get('state') or lookup[r].get('status')) not in terminal for r in reservation['runs']):
                    return None, 'This reservation already has live/unknown children. Use separate disjoint reservations for parallel writes.'
            if reservation:
                task = ('COORDINATED TASK ' + reservation['id'] + '\n'
                        'Other supervisors are working here. Your reserved scope (' + reservation['mode'] + '):\n'
                        + '\n'.join(reservation['paths']) + '\n'
                        'Do not modify outside this scope. If you need another path, STOP and report the dependency. '
                        'Do not delegate recursively.\n\n' + task)
            # Keyword-forward everything else (report_contract, resume_session,
            # profile/complexity/...) so it lands in the right parameter.
            spawn_kwargs.setdefault('task_key', (reservation or {}).get('task_key'))
            ticket = take_ticket()
            try:
                # Slow step, no shared state touched: let other supervisors read and
                # spawn while `claude --bg` starts. The ticket holds our slot.
                if release_on_spawn:
                    with unlocked():
                        run_id, error = original_spawn(task, allowed_tools, cwd, name, permission_mode,
                                   disallowed_tools, agent, announce_plan, **spawn_kwargs)
                else:
                    run_id, error = original_spawn(task, allowed_tools, cwd, name, permission_mode,
                                   disallowed_tools, agent, announce_plan, **spawn_kwargs)
            finally:
                drop_ticket(ticket)
            if run_id:
                board.attach(current_task, run_id)
            return run_id, error

    server._spawn_native_agent = spawn
    original_save = server._save_verified

    def save_verified(state):
        if current_task:
            state['coordination_task_id'] = current_task
            state['coordination_owner'] = owner
        original_save(state)

    server._save_verified = save_verified

    def continue_delegate(args):
        source, error = server._resolve_agent(args['run_id'])
        if error or not source:
            return server._error_result(error or 'Unknown agent')
        state = source.get('state') or source.get('status')
        if state not in ('done', 'completed', 'idle', 'failed', 'stopped'):
            return server._error_result('First stop_delegate and wait until settled. Cannot fork a live/blocked session safely.')
        if not source.get('sessionId'):
            return server._error_result('Source has no session ID')
        if not isinstance(args.get('message'), str) or not args['message'].strip():
            return server._error_result('message must be nonempty')
        run_id, error = spawn(args['message'], args.get('allowed_tools') or server.DEFAULT_ALLOWED_TOOLS,
                             source['cwd'], args.get('name') or 'local-followup',
                             resume_session=source['sessionId'])
        if error:
            return server._error_result(error)
        return result({'run_id': run_id, 'source_session': source['sessionId'],
                       'note': 'Forked native conversation on the local backend. Original transcript retained. Poll check_delegate_status.'})

    def pool_snapshot():
        """How much local capacity is actually free, by the same accounting the
        spawn admission uses -- so 'retry later' has a number behind it."""
        roster = server._agents_json()
        if roster is None:
            return {'known': False, 'note': 'native roster unavailable'}
        recorded = server._load_provenance()
        busy = ('done', 'completed', 'idle', 'failed', 'stopped')
        active = [a.get('id') for a in roster if a.get('kind') == 'background'
                  and (a.get('state') or a.get('status')) not in busy
                  and recorded.get(a.get('id'), {}).get('backend') == 'local']
        pending = len(_tickets())
        ceiling = server.LOCAL_SERVER_MAX_CONCURRENCY
        return {'known': True, 'max': ceiling, 'local_running': len(active),
                'spawning': pending, 'headroom': max(0, ceiling - len(active) - pending),
                'running_ids': active,
                'note': 'Only delegates recorded as local count; paid background sessions do not.'}

    def sync(args):
        data = board.sync(owner, args)
        data['pool'] = pool_snapshot()
        return result(data)

    def backend_info(args):
        from local_backend import inspect
        return result(inspect(server._default_settings_path()))

    server.PARENT_TOOLS.append(schema('local_backend_info',
        'Inspect the explicit local settings and LiteLLM route without invoking a model or exposing keys. '
        'Returns configured backend and whether all matching routes use hosted_vllm; this is not per-request tracing.', {}, []))
    server.TOOL_HANDLERS['local_backend_info'] = backend_info
    server.PARENT_TOOLS.extend(TOOLS)
    server.PARENT_TOOLS.append(schema('continue_delegate',
        'Continue a settled local conversation in a new background fork with its transcript context. '
        'For blocked/drifting work: stop_delegate, wait until settled, then call with your reply. '
        'Works from Claude and Codex without native SendMessage.',
        {'run_id': STRING, 'message': STRING, 'allowed_tools': STRING, 'name': STRING, 'task_id': STRING}, ['run_id', 'message']))
    server.TOOL_HANDLERS.update({
        'project_sync': sync,
        'task_claim': lambda args: result(board.claim(owner, args)),
        'task_update': lambda args: result(board.update(owner, args, settled)),
        'project_note': lambda args: result(board.note(owner, args)),
        'continue_delegate': continue_delegate,
    })
    for tool in server.PARENT_TOOLS:
        if tool['name'] in ('delegate_to_local', 'fan_out_to_local', 'delegate_verified'):
            tool['inputSchema']['properties']['task_id'] = {
                'type': 'string', 'description': 'Active task_claim reservation owned by this MCP session. Pass it for bookkeeping; the task always starts, and overlapping reservations are visible in project_sync but never block it.'}
        if 'SendMessage' in tool['description']:
            tool['description'] += ' In Codex use stop_delegate, wait until settled, then continue_delegate with the reply; native SendMessage is Claude-only.'

    original_handle = server.handle_request

    def handle(msg):
        nonlocal current_task, release_on_spawn
        if not isinstance(msg, dict):
            return server._response(None, None, {'code': -32600, 'message': 'Request must be an object'})
        if msg.get('method') == 'initialize':
            response = original_handle(msg)
            response['result']['serverInfo']['version'] = '0.8.0'
            response['result']['instructions'] = (
                'Shared local vLLM delegation for Claude Code and Codex. Worker runtime: Claude CLI. '
                'Before project work call project_sync then task_claim; a claim is always active. '
                'Pass task_id to delegation. Use task_update checkpoints and project_note for coordination. '
                'Reservations are visible in project_sync but never block; stale or blocking records do not hold follow-on work. '
                'Native SendMessage is unavailable in Codex: stop, wait, continue_delegate instead. '
                'Session identity: ' + owner)
            return response
        if msg.get('method') != 'tools/call':
            return original_handle(msg)
        try:
            params = msg.get('params') or {}
            args = params.get('arguments') or {}
            if not isinstance(args, dict):
                raise ValueError('arguments must be an object')
            current_task = args.get('task_id')
            name = params.get('name')
            # Reads mutate nothing here (project_sync's own SQLite transaction
            # arbitrates its snapshot), so they must not queue behind another
            # supervisor's 2-minute spawn. Only mutations take the file lock.
            if name in READ_ONLY_CALLS:
                return original_handle(msg)
            release_on_spawn = name in SPAWN_CALLS
            # Serialize mutations across stdio processes: two verified polls
            # must not create duplicate checkers/revisions or collide on .tmp.
            with locked():
                if params.get('name') in ('stop_delegate', 'continue_delegate'):
                    agent, _ = server._resolve_agent(args.get('run_id') or args.get('agent_id') or '')
                    run_owner = board.owner_for_run(agent.get('id')) if agent else None
                    if run_owner and run_owner != owner:
                        raise ValueError('Delegate belongs to another supervisor; request a handoff with project_note')
                if params.get('name') in ('check_verified_status', 'get_verified_result'):
                    vid = args.get('vid') or args.get('run_id')
                    if not isinstance(vid, str) or not server.re.fullmatch(r'[0-9a-f]{12}', vid):
                        raise ValueError('invalid vid')
                    state = server._load_verified(vid)
                    if state and state.get('coordination_task_id'):
                        if state.get('coordination_owner') != owner:
                            raise ValueError('Verified loop belongs to another session; use project_sync')
                        current_task = state['coordination_task_id']
                        if state.get('phase') not in ('passed', 'failed'):
                            board.authorize(owner, current_task, state['cwd'], True)
                return original_handle(msg)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            return server._response(msg.get('id'), server._error_result(str(exc)))
        finally:
            current_task = None
            release_on_spawn = False

    server.handle_request = handle
