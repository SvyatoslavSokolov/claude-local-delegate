# Local delegation: compact supervisor brief

Use this file for the normal path. Read `COORDINATION.md` only for conflicts,
handoffs, recovery, or multi-supervisor work. Read `TASK_DESIGN.md` only before a
nontrivial delegation whose briefing needs detailed acceptance criteria.

1. Call `project_sync(project=<absolute root>)`, keeping `next_event` for later
   incremental syncs. It returns your unfinished tasks and fresh ones of other
   sessions, only a count + short digest of idle foreign reservations
   (`stale_foreign`; they never block -- ignore them), three compact completed
   digests, counts, and at most 20 events by default.
2. Claim a stable task key with literal paths. Start the task this MCP session
   owns, and pass its `task_id` to every delegate.
3. Operating mode (2026-10-10): DEEP HELP, accuracy and speed first; tokens
   are not the constraint. The local model is Qwen3.8-Flash-Next (125B MoE,
   IQ3_S) on the Strata engine, 4x RTX 3090 at 192.168.1.109. Delegates talk to
   the engine DIRECTLY at :8080 (LiteLLM :4000 drops the mid-conversation
   system messages that carry SendMessage replies); sampling defaults live in
   Strata `POST /settings`, thinking effort in the profile's `effortLevel: high`.
   Native SendMessage works both ways with a running delegate (it reads at its
   next tool round): ask it questions, send corrections, let it ask you. It is a strong
   model: give it the work you would otherwise do yourself, not only lookups.
   ROLE (measured 2026-10-12 on 4 real virtual-hal fixes, blind, graded against
   the real commits): Opus stays on the critical path and owns decisions and final
   code; the local model is a parallel SECOND ENGINEER for verification and
   investigation. Never put it on your critical path and wait for it.
   | mode | result | wall time | use |
   | review_diff on a change with a planted bug | proved it (built the vhal image, ran the suite: 69 pass vs 1 fail) + 2 real issues the real commit missed, 0 false alarms | 15 min | ALWAYS, on every non-trivial change, while you go on |
   | start_expert + SendMessage questions | traced, cited answers; corrected my own reference by reading a sibling repo | map 5 min, then 1-3.5 min per answer | questions about code you are not reading yourself |
   | deep_dive on a symptom | root cause and the real fix's exact functions, plus a timing model | 15 min | open questions you would spend >10 min on |
   | delegate_to_local, small blind fix | identical to the real fix, hidden test passes | 15 min (Opus: ~1 min) | only queued work you are not waiting for |
   The protocol: at the start of a task, launch a deep_dive on each real unknown
   (and start_expert for a subsystem you will keep asking about); work; ask the
   experts instead of reading side code; before you call a change done,
   review_diff it and fold in what it proves. Its runs also read the project's
   auto-memory and sibling repos -- an asset, so keep that memory current.
   - `context_pack(task)` at the START of a nontrivial task: files/symbols with
     path:line, call/data flow, covering tests (run), history, traps, plan.
   - `deep_dive(question, context)` for any open question you would spend
     more than a few minutes on: it digs to the bottom, runs experiments,
     falsifies hypotheses and writes a dossier.
   - `review_diff(base, focus, test_command)` on EVERY non-trivial change before
     you call it done: an adversarial second opinion that tries to prove defects.
   - `start_expert(scope)`: a resident module expert; ask it with native
     SendMessage(to=<its name>, "Q1: ..."), the answer comes back as a
     cross-session message. Keep 2-5 experts, not dozens (10 cached conversations,
     ~2 GB host RAM each at ~130k tokens, one decode stream).
   - `project_findings` before a deep run: earlier dossiers may already answer it.
   `get_delegate_result` returns a deep run's WHOLE dossier (no tail compaction).
   Keep working while deep runs go; collect with wait_seconds=0 when you need the
   answer. Verify the claims you act on (the dossier cites path:line and the
   commands it ran, so spot-checking is cheap). AGY (`delegate_to_agy`) takes
   self-contained mid-size tasks; always report `agy --conversation <id>`.
   CAPACITY: the engine decodes ONE request at a time (~140-170 tok/s with MTP
   speculative decoding; measured 2026-10-10: batching 4 slots gave +4.6% total
   and 4x slower streams, so it stays off). Delegates share that one stream, so
   1-2 live deep runs is the sweet spot; more only queue. It keeps 10
   conversations parked in host RAM; beyond 10 live delegates every switch
   re-reads the whole context (~20 s per 120k tokens). `project_sync.pool.engine`
   and `pool.advice` show the live state (serving, queue, cached conversations);
   the `context_*`/`fits_now` figures are only a KV estimate.
   If the local backend is offline (the pool hook says so, or a spawn fails to
   connect), do not retry local delegation: work on the main model or AGY.
4. Brief one small outcome with exact files/anchors and an executable acceptance
   check. Workers must use Serena for code (the persona makes it mandatory); name
   the symbol and file so the first Serena lookup hits. For simulator work, ask
   for launch-in-background -> wait on a readiness marker -> check -> kill; never
   a fixed sleep or a foreground sim that holds the machine. Ask for a short result and name
   known dirty/forbidden paths. Split work before delegating if it spans
   unrelated files or more than about five steps.
5. Plan every delegation: pass `complexity` (trivial|small|medium|large),
   `est_minutes`, and `blocks` (task keys that cannot start before this one) so
   `python3 contrib/delegate_report.py --schedule` can build the queue and ETA.
   There is one model route (`default_model`, thinking on); `profile=fast` no
   longer selects a separate model (the old `local-fast` route does not exist on
   Strata and was removed from the profile).
6. Launch deep runs / delegates and keep working. Collect with
   `get_delegate_result(run_id, wait_seconds=0)` when you reach the point that
   needs the answer; block with a long wait (up to 900) only when you genuinely
   have nothing else to do. Native `ListAgents` shows the delegates and native
   `SendMessage` answers a `blocked` one; `watch_delegate` shows a run's
   narration when it seems to drift.
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
profile allows 237,568 tokens of context + 24,576 output (CLAUDE_CODE_MAX_CONTEXT_TOKENS /
CLAUDE_CODE_MAX_OUTPUT_TOKENS in vllm.delegate.settings.json; Strata delivers the full 262,144
window with the KV cache streamed to host RAM -- the old 180k cap was a vLLM limit). Deep runs get
a 300-turn ceiling; the 4-identical-calls repeat cap always applies. Every delegated task carries a
report contract asking for a concise final chat message (changes, verification actually run,
assumptions) with NO fixed line count in the prompt (removed 2026-09-16: a weak local model
conflated a numeric report-length cap with an unrelated length target inside the task itself,
e.g. "shrink this file to ~N lines", and looped trying to reconcile the two). Display-side
compaction of what the paid supervisor reads (DEFAULT_RESULT_LINES, default 60) is separate and
never truncates the transcript on disk or the model's own generation. Full transcripts and
detailed history stay on disk and are loaded only when a review requires them.
