"""The proactive guard sweep: it must run without a supervisor poll, touch only our delegates."""

import server


def test_only_tagged_local_delegates_are_guarded():
    assert server._is_guarded_delegate({"name": "⚡ [LOCAL] retime-plans"})
    assert server._is_guarded_delegate({"name": "⚡ campaign-live-r33"})
    assert server._is_guarded_delegate({"name": "\U0001f6e1️ [CHECK] verify"})
    assert server._is_guarded_delegate({"name": "\U0001f9ea [VERIFY] x"})
    assert server._is_guarded_delegate({"name": "\U0001f500 [FANOUT 1/5] y"})
    # the Gemini architect has its own budget; unrelated sessions are never touched
    assert not server._is_guarded_delegate({"name": "\U0001f9e0 [ARCH] design"})
    assert not server._is_guarded_delegate({"name": "codex session retrieval"})
    assert not server._is_guarded_delegate({"name": ""})


def test_sweep_checks_every_guarded_live_delegate_with_the_background_cap(monkeypatch):
    agents = [
        {"id": "a1", "name": "⚡ loop", "state": "working", "sessionId": "s1"},
        {"id": "a2", "name": "user chat", "state": "working", "sessionId": "s2"},
    ]
    seen = []
    monkeypatch.setattr(server, "_agents_json", lambda include_completed=True: agents)

    def fake_guard(agent, state, session_id, cap=None):
        seen.append((agent["id"], cap))
        return "TURN GUARD: killed" if agent["id"] == "a1" else None

    monkeypatch.setattr(server, "_turn_guard_check", fake_guard)
    notes = server._guard_sweep_once()
    assert seen == [("a1", server.BACKGROUND_MAX_TURNS)]  # a2 untouched
    assert notes == ["TURN GUARD: killed"]


def test_background_cap_is_above_the_poll_cap():
    # A live-sim campaign that blocks on one capture per turn crosses 40 honestly;
    # the unasked-for background sweep must not kill it on that basis alone.
    assert server.BACKGROUND_MAX_TURNS > server.DEFAULT_MAX_TURNS


def test_sweep_survives_a_broken_roster(monkeypatch):
    monkeypatch.setattr(server, "_agents_json", lambda include_completed=True: None)
    assert server._guard_sweep_once() == []


def test_guard_accepts_a_cap_override(monkeypatch):
    """A 60-turn run is under the background cap but over the poll cap."""
    monkeypatch.setattr(server, "_find_transcript", lambda sid: "/fake")
    monkeypatch.setattr(server, "_transcript_summary",
                        lambda tr: {"turns": 60, "tool_calls": [("Bash", "a"), ("Bash", "b")]})
    killed = []
    monkeypatch.setattr(server, "_stop_escalate", lambda agent, handle, mode: killed.append(agent.get("pid")))
    monkeypatch.setattr(server.metrics, "append_event", lambda *a, **k: None)
    agent = {"id": "run-cap-test", "pid": 4242}
    server._TURN_GUARD_KILLED.discard("run-cap-test")
    assert server._turn_guard_check(agent, "working", "s", cap=150) is None
    assert killed == []
    assert server._turn_guard_check(agent, "working", "s") is not None  # default 40
    assert killed == [4242]


def _isolate_caps(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_TURN_CAPS_FILE", str(tmp_path / "turn_caps.json"))


def test_per_run_cap_is_persisted_and_read_back(monkeypatch, tmp_path):
    _isolate_caps(monkeypatch, tmp_path)
    assert server._run_turn_cap("r1", 40) == 40            # nothing recorded -> default
    server._record_turn_cap("r1", 120)
    assert server._run_turn_cap("r1", 40) == 120
    assert server._run_turn_cap("other", 40) == 40
    server._record_turn_cap("r2", 0)
    assert server._run_turn_cap("r2", 40) == 0              # 0 = no turn ceiling


def test_a_live_campaign_with_a_raised_cap_survives_a_poll(monkeypatch, tmp_path):
    """85 turns is honest for a campaign that blocks on one capture per turn."""
    _isolate_caps(monkeypatch, tmp_path)
    server._record_turn_cap("campaign", 120)
    monkeypatch.setattr(server, "_find_transcript", lambda sid: "/fake")
    monkeypatch.setattr(server, "_transcript_summary",
                        lambda tr: {"turns": 85, "tool_calls": [("Bash", "a"), ("Bash", "b")]})
    killed = []
    monkeypatch.setattr(server, "_stop_escalate", lambda agent, handle, mode: killed.append(agent.get("pid")))
    monkeypatch.setattr(server.metrics, "append_event", lambda *a, **k: None)
    server._TURN_GUARD_KILLED.discard("campaign")
    assert server._turn_guard_check({"id": "campaign", "pid": 7}, "working", "s") is None
    assert killed == []


def test_repeat_cap_still_applies_with_no_turn_ceiling(monkeypatch, tmp_path):
    """max_turns=0 lifts the turn ceiling, never the identical-call loop detector."""
    _isolate_caps(monkeypatch, tmp_path)
    server._record_turn_cap("loop", 0)
    same = ("mcp__serena__find_symbol", '{"name_path_pattern": "_cla"}')
    monkeypatch.setattr(server, "_find_transcript", lambda sid: "/fake")
    monkeypatch.setattr(server, "_transcript_summary",
                        lambda tr: {"turns": 12, "tool_calls": [same] * 6})
    killed = []
    monkeypatch.setattr(server, "_stop_escalate", lambda agent, handle, mode: killed.append(agent.get("pid")))
    monkeypatch.setattr(server.metrics, "append_event", lambda *a, **k: None)
    server._TURN_GUARD_KILLED.discard("loop")
    note = server._turn_guard_check({"id": "loop", "pid": 9}, "working", "s")
    assert note is not None and "repeated the SAME tool call" in note
    assert killed == [9]


def test_only_one_process_becomes_the_sweeper(monkeypatch, tmp_path):
    """A dozen server copies must not all spawn `claude agents` every 20 s."""
    monkeypatch.setattr(server, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(server, "_GUARD_SWEEP_LOCK", str(tmp_path / "guard_sweep.lock"))
    first = server._acquire_sweep_lock()
    assert first is not None
    import subprocess, sys, textwrap
    probe = textwrap.dedent(f"""
        import fcntl, os
        fd = os.open({str(tmp_path / 'guard_sweep.lock')!r}, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); print("GOT")
        except OSError:
            print("BUSY")
    """)
    out = subprocess.run([sys.executable, "-c", probe], stdout=subprocess.PIPE).stdout.decode().strip()
    assert out == "BUSY"          # a second process cannot become a sweeper
    server.os.close(first)
    out = subprocess.run([sys.executable, "-c", probe], stdout=subprocess.PIPE).stdout.decode().strip()
    assert out == "GOT"           # released on close -> another copy can take over


def test_guard_stops_through_the_verified_path(monkeypatch, tmp_path):
    """A guard kill must use _stop_escalate (session re-discovery + proof), never a lone SIGTERM.

    Regression: a run the old guard reported as terminated at 203 turns kept
    running and rewrote plan files ~40 minutes later.
    """
    _isolate_caps(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_find_transcript", lambda sid: "/fake")
    monkeypatch.setattr(server, "_transcript_summary",
                        lambda tr: {"turns": 500, "tool_calls": [("Bash", "a"), ("Bash", "b")]})
    monkeypatch.setattr(server.metrics, "append_event", lambda *a, **k: None)
    calls, raw = [], []
    monkeypatch.setattr(server, "_stop_escalate",
                        lambda agent, handle, mode: calls.append((handle, mode)))
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: raw.append(pid))
    server._TURN_GUARD_KILLED.discard("zombie")
    assert server._turn_guard_check({"id": "zombie", "pid": 11}, "working", "s") is not None
    assert calls == [("zombie", "terminate")]
    assert raw == []          # no unverified signal on the happy path
