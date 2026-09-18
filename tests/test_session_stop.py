"""Session-lifecycle safety tests: proven, session-scoped stop_delegate
escalation, plus the six-session concurrency admission ceiling.

The observed failure these tests protect against: a stored roster PID is
unreliable -- a stopped runaway session reappeared under a DIFFERENT
`claude bg-pty-host` / `claude --resume` PID after the first signal. So the
stop must re-discover live process(es) by EXACT session id, escalate
SIGINT -> SIGTERM -> SIGKILL only while still alive, and prove liveness is
gone before reporting success, never touching another session.

Everything is deterministic: the roster and (for the escalation cases)
os.kill are mocked; the one end-to-end case runs a short harmless `sleep`
child created by the test and signals it for real. No model agent is ever
spawned.
"""
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))


def load_server(tmp, concurrency_env=None, grace_env='0'):
    """Import a fresh server module with state pointed at a temp dir.

    ``grace_env`` defaults to '0' so escalation tests do not sleep; pass None
    to leave the variable unset (built-in default) for config tests."""
    env = {'CLAUDE_LOCAL_DELEGATE_STATE_DIR': tmp}
    if grace_env is None:
        env.pop('CLAUDE_LOCAL_DELEGATE_STOP_GRACE_SECONDS', None)
    else:
        env['CLAUDE_LOCAL_DELEGATE_STOP_GRACE_SECONDS'] = grace_env
    if concurrency_env is None:
        env.pop('CLAUDE_LOCAL_DELEGATE_MAX_CONCURRENCY', None)
    else:
        env['CLAUDE_LOCAL_DELEGATE_MAX_CONCURRENCY'] = concurrency_env
    with patch.dict(os.environ, env, clear=False):
        sys.modules.pop('session_stop_server', None)
        spec = importlib.util.spec_from_file_location('session_stop_server', ROOT / 'server.py')
        s = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(s)
    s.STATE_DIR = tmp
    s.PROVENANCE_PATH = os.path.join(tmp, 'runs.json')
    s.VERIFIED_DIR = os.path.join(tmp, 'verified')
    s.BATCHES_DIR = os.path.join(tmp, 'batches')
    return s


def roster_entry(short_id, session_id, state='working', pid=None):
    return {'id': short_id, 'sessionId': session_id, 'kind': 'background',
            'state': state, 'pid': pid}


class StopDiscoveryTest(unittest.TestCase):
    """PID/session matching: exact session id only, live pids only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = load_server(self.tmp.name)
        self.roster = [
            roster_entry('aa11bb22', 'sess-1', pid=1111),
            roster_entry('cc33dd44', 'sess-2', pid=2222),
            roster_entry('ee55ff66', 'sess-3', 'completed', pid=3333),
            roster_entry('ff667788', 'sess-4', 'working', pid=None),
        ]
        self.addCleanup(patch.stopall)

    def _all_live(self, pids):
        patch.object(self.s, '_is_live_pid',
                     lambda pid: pid in pids).start()

    def test_discover_matches_exact_session_and_live_pids_only(self):
        self._all_live((1111, 2222, 3333))
        patch.object(self.s, '_agents_json', return_value=self.roster).start()
        self.assertEqual(self.s._discover_session_pids('sess-1'), [1111])
        # a DIFFERENT session's pid must never be returned
        self.assertEqual(self.s._discover_session_pids('sess-2'), [2222])
        # settled session -> nothing, even though it has a pid
        self.assertEqual(self.s._discover_session_pids('sess-3'), [])
        # no-pid entry / unknown session / empty -> nothing
        self.assertEqual(self.s._discover_session_pids('sess-4'), [])
        self.assertEqual(self.s._discover_session_pids('no-such-session'), [])
        self.assertEqual(self.s._discover_session_pids(''), [])

    def test_discover_ignores_prefix_or_substring_lookalikes(self):
        self._all_live((4444, 5555))
        lookalike = [
            roster_entry('bb22cc33', 'sess-1x', pid=4444),   # substring superset
            roster_entry('cc33dd44', 'x-sess-1', pid=5555),  # substring subset
        ]
        patch.object(self.s, '_agents_json', return_value=lookalike).start()
        self.assertEqual(self.s._discover_session_pids('sess-1'), [])

    def test_is_live_pid_probe_semantics(self):
        child = subprocess.Popen(['sleep', '5'])
        try:
            self.assertTrue(self.s._is_live_pid(child.pid))
        finally:
            child.kill(); child.wait(timeout=5)
        self.assertFalse(self.s._is_live_pid(child.pid))  # reaped -> gone
        # garbage pids are never live (a live garbage pid would loop escalation)
        self.assertFalse(self.s._is_live_pid(None))
        self.assertFalse(self.s._is_live_pid('not-a-pid'))

    def test_signal_pids_swallows_gone_but_propagates_permission(self):
        import unittest.mock as m
        with patch.object(self.s.os, 'kill') as kill:
            kill.side_effect = [ProcessLookupError(), None, None]
            self.assertEqual(self.s._signal_pids([11, 22, 33], signal.SIGINT), [22, 33])
        with patch.object(self.s.os, 'kill') as kill:
            kill.side_effect = PermissionError('not yours')
            with self.assertRaises(RuntimeError):
                self.s._signal_pids([11], signal.SIGINT)


class StopEscalationTest(unittest.TestCase):
    """Escalation: graceful success, full INT->TERM->KILL ladder, TERM-then-
    KILL in terminate mode, no collateral, terminal outcomes. os.kill is
    patched; no real process is signalled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = load_server(self.tmp.name)
        self.agent = roster_entry('aa11bb22', 'sess-1', pid=1111)
        self.kills = []
        # _die_on maps pid -> the one signal that actually ends it; any other
        # signal is ignored (the process survives it). Liveness is the mock:
        # a pid is alive until its fatal signal has landed.
        self._die_on = {}
        self._dead = set()
        patcher = patch.object(self.s.os, 'kill', self._fake_kill)
        patcher.start()
        patch.object(self.s, '_is_live_pid',
                     lambda pid: pid not in self._dead).start()
        self.addCleanup(patcher.stop)

    def _fake_kill(self, pid, sig):
        self.kills.append((pid, sig))
        if self._die_on.get(pid) == sig:
            self._dead.add(pid)

    def _cycling_roster(self, entries):
        """One roster per _agents_json call, clamped at the last entry."""
        state = {'i': 0}

        def fake(include_completed=True):
            state['i'] += 1
            return entries[min(state['i'] - 1, len(entries) - 1)]
        return fake

    def test_interrupt_settles_after_sigint_is_graceful_stop(self):
        self._die_on = {1111: signal.SIGINT}
        patch.object(self.s, '_agents_json', return_value=[self.agent]).start()
        patch.object(self.s, '_resolve_agent',
                     return_value=(self.agent, None)).start()
        result = self.s.stop_delegate({'run_id': 'aa11bb22'})
        text = result['content'][0]['text']
        self.assertFalse(result['isError'])
        self.assertIn('graceful-stop', text)
        self.assertIn('VERIFIED liveness is gone', text)
        # first signal is SIGINT to the discovered pid; never escalates
        self.assertEqual(self.kills, [(1111, signal.SIGINT)])

    def test_terminate_mode_begins_with_sigterm(self):
        self._die_on = {1111: signal.SIGTERM}
        patch.object(self.s, '_agents_json', return_value=[self.agent]).start()
        patch.object(self.s, '_resolve_agent',
                     return_value=(self.agent, None)).start()
        result = self.s.stop_delegate({'run_id': 'aa11bb22', 'mode': 'terminate'})
        text = result['content'][0]['text']
        self.assertIn('terminated', text)
        self.assertEqual(self.kills, [(1111, signal.SIGTERM)])
        self.assertNotIn(signal.SIGINT, [sig for _, sig in self.kills])

    def test_full_ladder_with_pid_respawn_is_killed(self):
        # The runaway case: after each signal the session REAPPEARS under a
        # new pid (1111 -> 2222 -> 3333). INT did not settle it, nor did
        # TERM; only SIGKILL ends it, and success is claimed only after the
        # final re-discovery finds nothing live.
        self._die_on = {1111: signal.SIGINT, 2222: signal.SIGTERM,
                        3333: signal.SIGKILL}
        entries = [
            [roster_entry('aa11bb22', 'sess-1', pid=1111)],
            [roster_entry('aa11bb22', 'sess-1', pid=2222)],
            [roster_entry('aa11bb22', 'sess-1', pid=3333)],
        ]
        patch.object(self.s, '_agents_json',
                     side_effect=self._cycling_roster(entries)).start()
        patch.object(self.s, '_resolve_agent',
                     return_value=(self.agent, None)).start()
        result = self.s.stop_delegate({'run_id': 'aa11bb22'})
        text = result['content'][0]['text']
        self.assertFalse(result['isError'])
        self.assertIn('killed', text)
        self.assertIn('VERIFIED liveness is gone', text)
        # the exact escalation ladder on the exact session pids, in order
        self.assertEqual(self.kills, [
            (1111, signal.SIGINT), (2222, signal.SIGTERM), (3333, signal.SIGKILL),
        ])

    def test_terminate_then_kill_when_still_alive(self):
        # terminate mode starts at SIGTERM: 1111 survives it, reappears as
        # 2222, and only SIGKILL settles the session.
        self._die_on = {1111: signal.SIGTERM, 2222: signal.SIGKILL}
        entries = [
            [roster_entry('aa11bb22', 'sess-1', pid=1111)],
            [roster_entry('aa11bb22', 'sess-1', pid=2222)],
        ]
        patch.object(self.s, '_agents_json',
                     side_effect=self._cycling_roster(entries)).start()
        patch.object(self.s, '_resolve_agent',
                     return_value=(self.agent, None)).start()
        result = self.s.stop_delegate({'run_id': 'aa11bb22', 'mode': 'terminate'})
        text = result['content'][0]['text']
        self.assertFalse(result['isError'])
        self.assertIn('killed', text)
        self.assertEqual(self.kills, [(1111, signal.SIGTERM),
                                      (2222, signal.SIGKILL)])

    def test_already_settled_signals_nothing(self):
        pass  # roster entry is settled; liveness is irrelevant here
        agent = roster_entry('aa11bb22', 'sess-1', 'working', pid=1111)
        patch.object(self.s, '_agents_json',
                     return_value=[roster_entry('aa11bb22', 'sess-1',
                                                'completed', pid=1111)]).start()
        patch.object(self.s, '_resolve_agent', return_value=(agent, None)).start()
        result = self.s.stop_delegate({'run_id': 'aa11bb22'})
        text = result['content'][0]['text']
        self.assertFalse(result['isError'])
        self.assertIn('already-settled', text)
        self.assertEqual(self.kills, [])

    def test_no_collateral_signal_to_other_session(self):
        other = roster_entry('cc33dd44', 'sess-2', pid=2222)
        self._die_on = {1111: signal.SIGINT}
        patch.object(self.s, '_agents_json',
                     return_value=[self.agent, other]).start()
        patch.object(self.s, '_resolve_agent',
                     return_value=(self.agent, None)).start()
        result = self.s.stop_delegate({'run_id': 'aa11bb22'})
        self.assertFalse(result['isError'])
        # every signal is aimed at the target session pid only
        for pid, _sig in self.kills:
            self.assertEqual(pid, 1111)
        self.assertNotIn(2222, [pid for pid, _ in self.kills])

    def test_surviving_pid_after_full_ladder_is_an_error(self):
        self._die_on = {}  # every signal is ignored: the pid survives forever
        patch.object(self.s, '_agents_json',
                     return_value=[self.agent]).start()
        patch.object(self.s, '_resolve_agent',
                     return_value=(self.agent, None)).start()
        result = self.s.stop_delegate({'run_id': 'aa11bb22'})
        self.assertTrue(result['isError'])
        text = result['content'][0]['text']
        self.assertIn('Could NOT stop', text)
        self.assertNotIn('graceful-stop', text)
        self.assertNotIn('killed', text)
        # full ladder was attempted on the session pid, in order
        self.assertEqual(self.kills,
                         [(1111, signal.SIGINT), (1111, signal.SIGTERM),
                          (1111, signal.SIGKILL)])

    def test_invalid_mode_rejected(self):
        result = self.s.stop_delegate({'run_id': 'aa11bb22', 'mode': 'yell'})
        self.assertTrue(result['isError'])
        self.assertEqual(self.kills, [])


class StopEndToEndTest(unittest.TestCase):
    """REAL OS child, REAL signals (os.kill not patched): the escalation
    genuinely proves liveness is gone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = load_server(self.tmp.name, grace_env='0.1')
        # A short harmless child that TRAPS SIGINT (so the graceful step is
        # genuinely not enough) and dies to the escalated SIGTERM. os.kill and
        # the signal-0 liveness probe are the REAL ones here.
        self.child = subprocess.Popen([
            'python3', '-c',
            'import signal, time; signal.signal(signal.SIGINT, '
            'signal.SIG_IGN); time.sleep(30)'])
        # Let the child install its SIGINT handler before the stop ladder sends
        # its first signal; otherwise a fast parent can win the startup race.
        time.sleep(0.05)
        pid = self.child.pid

        def fake_live(p):
            # poll() reaps the child once it dies; an unreaped zombie would
            # pass a signal-0 probe and look alive forever.
            self.child.poll()
            return self.child.poll() is None

        def fake_agents_json(include_completed=True):
            # the roster still lists the session as working; the discovery's
            # liveness filter (the real probe, via fake_live) decides
            return [roster_entry('aa11bb22', 'sess-e2e', 'working', pid=pid)]
        patch.object(self.s, '_resolve_agent',
                     return_value=(roster_entry('aa11bb22', 'sess-e2e',
                                                'working', pid=pid), None)).start()
        patch.object(self.s, '_is_live_pid', side_effect=fake_live).start()
        patch.object(self.s, '_agents_json', side_effect=fake_agents_json).start()
        self.addCleanup(patch.stopall)

    def test_sigint_then_sigterm_and_proven_gone(self):
        result = self.s.stop_delegate({'run_id': 'aa11bb22'})
        text = result['content'][0]['text']
        self.assertFalse(result['isError'])
        # SIGINT did not settle it (the child traps INT); the ladder must
        # escalate to SIGTERM and then prove the pid is gone.
        self.assertIn('terminated', text)
        self.assertIn('VERIFIED liveness is gone', text)
        self.child.wait(timeout=5)
        self.assertIsNotNone(self.child.poll())
        # the real process is actually gone: a signal-0 probe ESRCHes
        with self.assertRaises(ProcessLookupError):
            os.kill(self.child.pid, 0)


class ConcurrencyCeilingTest(unittest.TestCase):
    """LOCAL_SERVER_MAX_CONCURRENCY still exists (default 6, validated
    override) for status reporting, but the admission gate that used to
    refuse a spawn/fan-out at/over it was removed (2026-09-16): it counted
    `blocked` sessions -- waiting on a reply, no live vLLM inference -- the
    same as `working` ones, so an unrelated blocked backlog could report the
    pool "full" while the GPU was idle. A spawn is never refused here now."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = load_server(self.tmp.name)  # default ceiling
        self._real_pool_usage = self.s._local_pool_usage
        self._active = 0

        def fake_usage():
            return [f'active-{i}' for i in range(self._active)], True
        self.spawn_calls = []

        def fake_spawn(task, allowed_tools, cwd, name, *a, **k):
            self.spawn_calls.append(name)
            return f'new-{len(self.spawn_calls):04d}', None
        patch.object(self.s, '_local_pool_usage', side_effect=fake_usage).start()
        patch.object(self.s, '_spawn_native_agent', side_effect=fake_spawn).start()
        self.addCleanup(patch.stopall)

    def test_default_ceiling_is_six(self):
        self.assertEqual(self.s.LOCAL_SERVER_MAX_CONCURRENCY, 6)

    def test_override_must_be_positive(self):
        with tempfile.TemporaryDirectory() as tmp2:
            self.assertEqual(load_server(tmp2, '3').LOCAL_SERVER_MAX_CONCURRENCY, 3)
            self.assertEqual(load_server(tmp2, '0').LOCAL_SERVER_MAX_CONCURRENCY, 6)
            self.assertEqual(load_server(tmp2, '-2').LOCAL_SERVER_MAX_CONCURRENCY, 6)
            self.assertEqual(load_server(tmp2, 'garbage').LOCAL_SERVER_MAX_CONCURRENCY, 6)

    def test_seventh_single_start_admitted_gate_removed(self):
        # Admission gate was removed (2026-09-16): a `blocked` session (waiting
        # on a reply, zero live vLLM inference) was being counted the same as
        # a `working` one, so an unrelated backlog could report "pool full"
        # while the GPU was idle. A spawn at/over the old ceiling is no longer
        # refused.
        self._active = 6
        result = self.s.start_delegate({'task': 'one more', 'profile': 'think'})
        self.assertFalse(result['isError'])
        self.assertEqual(len(self.spawn_calls), 1)

    def test_single_start_admitted_under_ceiling(self):
        self._active = 5
        result = self.s.start_delegate({'task': 'go', 'profile': 'think'})
        self.assertFalse(result['isError'])
        self.assertEqual(len(self.spawn_calls), 1)

    def test_fanout_admitted_over_old_headroom_gate_removed(self):
        self._active = 4
        result = self.s.fan_out_to_local({'items': ['a', 'b', 'c'],
                                          'shared_instruction': 'do x',
                                          'profile': 'think'})
        self.assertFalse(result['isError'])
        self.assertEqual(len(self.spawn_calls), 3)

    def test_fanout_partial_headroom_admitted_gate_removed(self):
        self._active = 5
        result = self.s.fan_out_to_local({'items': ['a', 'b'],
                                          'shared_instruction': 'do x'})
        self.assertFalse(result['isError'])
        self.assertEqual(len(self.spawn_calls), 2)

    def test_fanout_exactly_headroom_admitted(self):
        self._active = 4
        result = self.s.fan_out_to_local({'items': ['a', 'b'],
                                          'shared_instruction': 'do x',
                                          'profile': 'think'})
        self.assertFalse(result['isError'])
        self.assertEqual(len(self.spawn_calls), 2)

    def test_unreadable_roster_no_longer_refuses_spawn(self):
        patcher = patch.object(self.s, '_local_pool_usage',
                               return_value=([], False))
        patcher.start()
        self.addCleanup(patcher.stop)
        result = self.s.start_delegate({'task': 'go', 'profile': 'think'})
        self.assertFalse(result['isError'])
        self.assertEqual(len(self.spawn_calls), 1)

    def test_stale_blocked_roster_entry_without_live_pid_frees_slot(self):
        roster = [
            roster_entry('stale', 'old-session', 'blocked', pid=1111),
            roster_entry('live', 'current-session', 'blocked', pid=2222),
            roster_entry('paid', 'paid-session', 'blocked', pid=3333),
        ]
        self.s._record_provenance('stale', {'backend': 'local'})
        self.s._record_provenance('live', {'backend': 'local'})
        # A paid/non-local session does not consume the vLLM pool even if live.
        self.s._record_provenance('paid', {'backend': 'hosted'})
        with patch.object(self.s, '_agents_json', return_value=roster), \
             patch.object(self.s, '_is_live_pid', side_effect=lambda pid: pid in (2222, 3333)):
            active, readable = self._real_pool_usage()
        self.assertTrue(readable)
        self.assertEqual(active, ['live'])


class StopGraceConfigTest(unittest.TestCase):
    def test_grace_default_override_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_server(tmp, grace_env=None).STOP_GRACE_SECONDS, 5.0)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_server(tmp, grace_env='2.5').STOP_GRACE_SECONDS, 2.5)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_server(tmp, grace_env='bad').STOP_GRACE_SECONDS, 5.0)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_server(tmp, grace_env='-1').STOP_GRACE_SECONDS, 5.0)


if __name__ == '__main__':
    unittest.main()
