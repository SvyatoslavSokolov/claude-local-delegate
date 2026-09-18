# Analytics: the "improve the pipeline" loop

How to measure the health of the delegation pipeline and act on it. This is the
self-sufficient, stdlib-only analytics layer — no dashboard, no server running,
no third-party deps. One command, plus a flat CSV you can feed to `pandas`.

## The one command

```bash
python3 contrib/report.py                 # last 30 days, printed to terminal
python3 contrib/report.py --days 0        # all history
python3 contrib/report.py --days 7
```

It reads the state dir (`~/.claude-local-delegate/`, override with
`--state-dir`) **read-only** through the shared `metrics.py` readers and prints
five sections:

1. **Cohort** — total runs, rated runs, rating rate %, date span, blocked runs.
2. **Trend** — p50 `quality` / p50 `worth_it` per ISO week (over rated runs).
   This is the "is delegation getting better or worse" line.
3. **Health-index** — the pipeline areas, each with a `[FLAG]` when a threshold
   is crossed:
   - **blocked by category** (hook-gate, mcp-tool-missing, websearch-broken, …)
   - **verified revise-rate** (share of `delegate_verified` cycles that needed a revision)
   - **fan-out batch fail-rate** (share of `fan_out` batches with zero agents)
   - **delta vs baseline** (p50 duration_s / api_calls / output_tokens / peak_context vs a baseline)
   - **complexity × profile** cells (n, p50 est vs actual, ratio, mean quality, mean worth_it)
4. **Insights** — the actual "close the loop" output: a sorted **problem →
   named action** list, e.g.
   `large/think mean worth_it 15 (<50) -> re-route this class to a better profile or split the task`.
5. **Lowest worth_it** — the 10 worst rated runs with their note, so you can see
   *why* the bad ones were bad.

## The flat CSV (for pandas)

```bash
python3 contrib/report.py --days 0 --csv delegations.csv
```

One row per delegation, plain columns, **no nested JSON** — `pd.read_csv` loads
it directly. This is the persistent per-task `quality`/`worth_it` data. Columns:

`run_id, task_id, task_key, name, project, cwd, created_at, finished_at,
duration_s, complexity, est_minutes, profile, model, read_only, prompt_chars,
quality, worth_it, api_calls, output_tokens, input_tokens, cached_input_tokens,
peak_context, thinking_chars, text_chars, tool_calls, blocked_category,
n_blocked, verified, verified_iterations, batch_id, task_status`

Examples:

```python
import pandas as pd
df = pd.read_csv("delegations.csv")

# trend of worth_it over time
df.assign(week=pd.to_datetime(df.created_at, unit="s").dt.to_period("W").dt.to_timestamp())
   .groupby("week").worth_it.median()

# is delegation paying off per class?
df.groupby(["complexity", "profile"])["worth_it"].mean().sort_values()

# long runs with low worth -> candidates to re-route or split
df[(df.duration_s > 30*60) & (df.worth_it < 50)]
```

`quality` and `worth_it` are 0–100 and only present on rated runs (empty
otherwise) — they are the core of the quality model and are preserved as real
columns, not buried.

## Machine-readable output

```bash
python3 contrib/report.py --json          # the full report dict (cohort/trend/health/insights/lowest)
python3 contrib/report.py --md out.md     # the terminal body as markdown
```

`--json` is the scripting surface; the same dict the terminal renders.

## Thresholds (tune in one place)

All thresholds are **module-level constants at the top of `contrib/report.py`**
— change them there, do not hunt through the code:

```python
BLOCKED_PCT    = 15    # flag a blocked category above this % of all runs
REVISE_PCT     = 30    # flag verified revise rate above this %
BATCH_FAIL_PCT = 20    # flag share of zero-agent batches above this %
DELTA_PCT      = 25    # flag |p50 vs baseline| delta above this %
LOW_WORTH      = 50    # flag a complexity x profile cell below this worth_it
OVERRUN_RATIO  = 1.5   # flag cells whose p50 actual/est ratio exceeds this
BASELINE_DEFAULT = "804,19,25000,67000"  # p50 duration_s, api_calls, output_tokens, peak_context
```

Pass a different baseline at runtime: `python3 contrib/report.py --baseline 804,19,25000,67000`.

A **negative** baseline delta means recent runs are *smaller/faster* than the
baseline (e.g. after a `--tools` grant cut fixed overhead) — that is a good
insight, not a fault. The flag fires on magnitude, and the direction is shown.

## Where the data comes from (and the one new write)

- **`spawn`** event — planning metadata, written at spawn (`server.py`).
- **`rate`** event — the two scores + transcript stats, written by `rate_delegate`.
- **`blocked`** event — the blocked category, written by `server.py` on the
  *transition into* the blocked state (once per episode, deduped by run id,
  cleared when the run leaves blocked). This was the one signal that was
  previously classified per-poll and thrown away; it is now persisted so the
  report can aggregate it.
- **`verified/*.json`** and **`batches/*.json`** — already on disk from the
  verified loop and fan-out; `report.py` reads them (they were previously never
  read by any analytics tool).
- **`coordination.sqlite3`** — read read-only to map a run to its task's status.

All reads are best-effort and the tool **never raises** on missing data — absent
state dir, empty ledger, missing sqlite, or empty `verified/`/`batches/` all
degrade to "no data" lines and exit 0.

## Related tools (not this loop)

- `contrib/delegate_report.py` — per complexity×profile aggregates over the
  ledger + list-scheduling ETA (`--schedule`). Complements `report.py`
  (forward-looking schedule vs backward-looking health).
- `contrib/history_stats.py` — percentiles over the historical session cohort
  (`session-starts.json`), independent of the metrics ledger.
- `analysis/analyze.py` — deeper per-session categorization + `diagnose()` rules.

These all self-locate the repo root and read the same state dir; `report.py` is
the one that closes the loop with insights + the flat CSV.