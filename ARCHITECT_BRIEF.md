# Local delegation: compact supervisor brief

Use this file for the normal path. Read `COORDINATION.md` only for conflicts,
handoffs, recovery, or multi-supervisor work. Read `TASK_DESIGN.md` only before a
nontrivial delegation whose briefing needs detailed acceptance criteria.

1. Call `project_sync(project=<absolute root>)`, keeping `next_event` for later
   incremental syncs. It returns all unfinished work, only three compact completed
   digests, counts, and at most 20 events by default.
2. Claim a stable task key with literal paths. Start only an active task owned by
   this MCP session, and pass its `task_id` to every delegate.
3. Keep architecture, task boundaries, security decisions, and final review on the
   main model. Delegate mechanical work that saves more effort than spawn/review:
   focused extraction, one-file edits, repetitive changes, and concrete checks.
4. Brief one small outcome with exact files/anchors and an executable acceptance
   check. Ask for a short result and name known dirty/forbidden paths. Split work
   before delegating if it spans unrelated files or more than about five steps.
5. Normal path: `delegate_to_local(...)`, then exactly one
   `get_delegate_result(run_id, wait_seconds=<this client's max>)` -- the tool
   description states that ceiling (900 in Claude Code, 220 in Codex). Half of all
   runs exceed 10 minutes, so a smaller wait only buys extra paid round-trips.
   Repeat only after timeout.
   `check_delegate_status` and `watch_delegate` are diagnostic tools for blocked,
   drifting, or unusually long work; routine polling spends main-model turns.
6. Leave `announce_plan=false` unless actively supervising a risky run. Use
   `delegate_verified` only for changes with objective acceptance criteria; its
   checker/revision state machine costs additional local sessions.
7. Review the compact result, actual diff, and relevant checks. Finish with
   `task_update(status="done", note=<short evidence>)` and incremental
   `project_sync(after_event=<cursor>)`.

Historical baseline (604 known local sessions, 493 readable; regenerate with
`python3 contrib/history_stats.py --pretty`): median duration 719s, 81 model
turns, 60k peak context, and 27 final lines; p95 peak context 191k. The delegate
profile is capped at 262,144 context tokens, covering 97.2% of this history before
compaction while limiting KV-cache outliers. Every delegated task carries a report contract asking for at most 30 lines of
substance (changes, verification actually run, assumptions), so the default
compaction (first 4 + last 30 lines) normally never fires. Full transcripts and detailed history stay on disk and are loaded
only when a review requires them.
