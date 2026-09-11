# Local delegation: compact supervisor brief

Use this file for the normal path. Read `COORDINATION.md` only for conflicts,
handoffs, recovery, or multi-supervisor work. Read `TASK_DESIGN.md` only before a
nontrivial delegation whose briefing needs detailed acceptance criteria.

1. Call `project_sync(project=<absolute root>)`, keeping `next_event` for later
   incremental syncs. It returns all unfinished work, only three compact completed
   digests, counts, and at most 20 events by default.
2. Claim a stable task key with literal paths. Start the task this MCP session
   owns, and pass its `task_id` to every delegate.
3. Keep only task framing, architecture, security decisions, and concise final
   review on the main model. Delegate every substantive project action first,
   including research, implementation, edits, and checks. Do not duplicate the
   delegate's work while it runs. Prefer one focused worker unless independent
   parallel tasks are necessary, and request full output only when compact results
   and the diff are insufficient.
4. Brief one small outcome with exact files/anchors and an executable acceptance
   check. Ask for a short result and name known dirty/forbidden paths. Split work
   before delegating if it spans unrelated files or more than about five steps.
5. Plan every delegation: pass `complexity` (trivial|small|medium|large),
   `est_minutes`, and `blocks` (task keys that cannot start before this one) so
   `python3 contrib/delegate_report.py --schedule` can build the queue and ETA.
   Decide thinking per task with `profile`: `fast` (thinking off) when the steps
   are fully specified -- exact files and edits, renames, mechanical refactors,
   running a named command and reporting, collecting data by a given recipe;
   `think` when the worker must find a cause, choose between designs, or plan a
   multi-file change. Default: trivial/small -> fast, medium/large -> think.
   `fast` needs `CLAUDE_LOCAL_DELEGATE_FAST_MODEL` (a thinking-off gateway route);
   without it the run uses thinking and the ledger records `think`.
6. Normal path: `delegate_to_local(...)`, then exactly one
   `get_delegate_result(run_id, wait_seconds=<this client's max>)` -- the tool
   description states that ceiling (900 in both Claude Code and Codex). Half of all
   runs exceed 10 minutes, so a smaller wait only buys extra paid round-trips.
   Repeat only after timeout.
   `check_delegate_status` and `watch_delegate` are diagnostic tools for blocked,
   drifting, or unusually long work; routine polling spends main-model turns.
7. Leave `announce_plan=false` unless actively supervising a risky run. Use
   `delegate_verified` only for changes with objective acceptance criteria; its
   checker/revision state machine costs additional local sessions.
8. Review the compact result, actual diff, and relevant checks, then call
   `rate_delegate(run_id, quality, worth_it, note)`. quality: 0 = not what was
   asked, 50 = what you would have produced, 100 = far better than you.
   worth_it: 0 = doing it yourself was cheaper in main-model tokens and time,
   100 = delegation clearly paid off. The ledger (`metrics.jsonl`) also stores
   duration, API calls, output tokens and thinking share. Finish with
   `task_update(status="done", note=<short evidence>)` and incremental
   `project_sync(after_event=<cursor>)`.

Historical baseline (704 known local sessions, 579 readable; regenerate with
`python3 contrib/history_stats.py --pretty`, schema 2 counts one API response
once): median duration 804s, 19 model calls, 25k output tokens (~69% of it
thinking), 67k peak context; p95 peak context 154k. That history carried ~20-31k
tokens of fixed system/tool overhead per call; spawns now pass `--tools` with only
the granted built-ins, which measured ~3.7k. Grant only the tools a task needs. The delegate
profile is capped at 262,144 context tokens, covering 97.2% of this history before
compaction while limiting KV-cache outliers. Every delegated task carries a report contract asking for at most 30 lines of
substance (changes, verification actually run, assumptions), so the default
compaction (first 4 + last 30 lines) normally never fires. Full transcripts and detailed history stay on disk and are loaded
only when a review requires them.
